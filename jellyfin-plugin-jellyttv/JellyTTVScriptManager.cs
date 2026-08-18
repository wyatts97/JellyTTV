using System;
using System.Linq;
using System.Reflection;
using System.Runtime.Loader;
using Microsoft.Extensions.Logging;
using Newtonsoft.Json.Linq;

namespace Jellyfin.Plugin.JellyTTV;

/// <summary>
/// Handles injection of the JellyTTV client script into the Jellyfin web UI.
/// Prefers established third-party plugins when available:
///   1. JavaScript Injector (programmatic script registration)
///   2. File Transformation (content transformation registration)
/// Falls back to the built-in IStartupFilter middleware otherwise.
/// </summary>
public sealed class JellyTTVScriptManager : IDisposable
{
    private static readonly Guid PluginId = Guid.Parse("a1b2c3d4-e5f6-7890-abcd-ef1234567890");

    private readonly ILogger<JellyTTVScriptManager> _logger;
    private readonly CancellationTokenSource _cts = new();
    private bool _disposed;

    /// <summary>
    /// Initializes a new instance of the <see cref="JellyTTVScriptManager"/> class
    /// and attempts to register with an external plugin.
    /// </summary>
    public JellyTTVScriptManager(ILogger<JellyTTVScriptManager> logger)
    {
        _logger = logger;

        // Try immediately in case JS Injector is already loaded.
        if (TryExternalRegistration())
        {
            return;
        }

        // Otherwise retry in the background; JS Injector may not be ready at startup.
        _ = RetryExternalRegistrationAsync(_cts.Token);
    }

    private async Task RetryExternalRegistrationAsync(CancellationToken cancellationToken)
    {
        for (var attempt = 1; attempt <= 5; attempt++)
        {
            _logger.LogDebug("JellyTTV external registration attempt {Attempt}", attempt);
            try
            {
                await Task.Delay(TimeSpan.FromSeconds(2 * attempt), cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }

            try
            {
                if (TryExternalRegistration())
                {
                    _logger.LogInformation("JellyTTV external registration succeeded on attempt {Attempt}", attempt);
                    return;
                }
            }
            catch (Exception ex)
            {
                _logger.LogDebug(ex, "JellyTTV external registration attempt {Attempt} failed", attempt);
            }
        }

        _logger.LogWarning("JellyTTV could not register with an external injection plugin; relying on built-in middleware");
    }

    /// <inheritdoc />
    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        _cts.Cancel();
        _cts.Dispose();
    }

    private bool TryExternalRegistration()
    {
        try
        {
            if (TryRegisterWithJavaScriptInjector())
            {
                _logger.LogInformation("JellyTTV registered with JavaScript Injector plugin");
                IsExternal = true;
                return true;
            }
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "JavaScript Injector registration failed");
        }

        try
        {
            if (TryRegisterWithFileTransformation())
            {
                _logger.LogInformation("JellyTTV registered with File Transformation plugin");
                IsExternal = true;
                return true;
            }
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "File Transformation registration failed");
        }

        _logger.LogInformation("No external injection plugin found; using built-in middleware");
        IsExternal = false;
        return false;
    }

    /// <summary>
    /// Gets a value indicating whether an external plugin is handling the injection.
    /// </summary>
    public bool IsExternal { get; private set; }

    /// <summary>
    /// Gets the version of the current assembly.
    /// </summary>
    public static string Version
    {
        get
        {
            var v = typeof(JellyTTVScriptManager).Assembly.GetName().Version;
            return v == null ? "0.1.0" : $"{v.Major}.{v.Minor}.{v.Build}";
        }
    }

    /// <summary>
    /// Gets the HTML injection markup for the built-in middleware fallback.
    /// </summary>
    public static string GetInjectionHtml()
    {
        return $@"
<!-- JellyTTV Plugin BEGIN -->
<link rel=""stylesheet"" href=""JellyTTV/twitch.css?v={Version}"" />
<script type=""text/javascript"" src=""JellyTTV/twitch.js?v={Version}""></script>
<!-- JellyTTV Plugin END -->
";
    }

    private bool TryRegisterWithJavaScriptInjector()
    {
        var assembly = AssemblyLoadContext.All
            .SelectMany(x => x.Assemblies)
            .FirstOrDefault(x => x.FullName?.Contains("Jellyfin.Plugin.JavaScriptInjector") ?? false);

        if (assembly == null)
        {
            return false;
        }

        var type = assembly.GetType("Jellyfin.Plugin.JavaScriptInjector.PluginInterface");
        if (type == null)
        {
            _logger.LogWarning("JavaScript Injector found but PluginInterface type is missing");
            return false;
        }

        var script = $@"
(function() {{
    var style = document.createElement('link');
    style.rel = 'stylesheet';
    style.href = 'JellyTTV/twitch.css?v={Version}';
    document.head.appendChild(style);

    var s = document.createElement('script');
    s.type = 'text/javascript';
    s.src = 'JellyTTV/twitch.js?v={Version}';
    document.head.appendChild(s);
}})();";

        var registration = new JObject
        {
            { "id", $"{PluginId}-twitch" },
            { "name", "JellyTTV Twitch" },
            { "script", script },
            { "enabled", true },
            { "requiresAuthentication", false },
            { "pluginId", PluginId.ToString() },
            { "pluginName", "JellyTTV" },
            { "pluginVersion", Version }
        };

        var method = type.GetMethod("RegisterScript", BindingFlags.Public | BindingFlags.Static);
        if (method == null)
        {
            _logger.LogWarning("JavaScript Injector PluginInterface.RegisterScript not found");
            return false;
        }

        var result = method.Invoke(null, new object[] { registration });
        return result is true;
    }

    private bool TryRegisterWithFileTransformation()
    {
        var assembly = AssemblyLoadContext.All
            .SelectMany(x => x.Assemblies)
            .FirstOrDefault(x => x.FullName?.Contains(".FileTransformation") ?? false);

        if (assembly == null)
        {
            return false;
        }

        var type = assembly.GetType("Jellyfin.Plugin.FileTransformation.PluginInterface");
        if (type == null)
        {
            _logger.LogWarning("File Transformation found but PluginInterface type is missing");
            return false;
        }

        var payload = new JObject
        {
            { "id", PluginId.ToString() },
            { "fileNamePattern", @"index\.html$" },
            { "callbackAssembly", typeof(JellyTTVScriptManager).Assembly.FullName },
            { "callbackClass", typeof(FileTransformationHelper).FullName },
            { "callbackMethod", nameof(FileTransformationHelper.InjectClientScript) }
        };

        var method = type.GetMethod("RegisterTransformation", BindingFlags.Public | BindingFlags.Static);
        if (method == null)
        {
            _logger.LogWarning("File Transformation PluginInterface.RegisterTransformation not found");
            return false;
        }

        var result = method.Invoke(null, new object?[] { payload });
        return result is true;
    }
}

/// <summary>
/// Callback class invoked by the File Transformation plugin to modify index.html.
/// </summary>
public static class FileTransformationHelper
{
    /// <summary>
    /// Injects the JellyTTV client script and stylesheet into the provided HTML contents.
    /// </summary>
    public static string InjectClientScript(object? content)
    {
        if (content == null)
        {
            return string.Empty;
        }

        var html = string.Empty;
        if (content is JObject jobj)
        {
            html = jobj["contents"]?.ToString() ?? string.Empty;
        }
        else
        {
            var contentsProperty = content.GetType().GetProperty("contents", BindingFlags.Public | BindingFlags.Instance | BindingFlags.IgnoreCase);
            if (contentsProperty != null)
            {
                html = contentsProperty.GetValue(content)?.ToString() ?? string.Empty;
            }
        }

        if (string.IsNullOrEmpty(html))
        {
            return string.Empty;
        }

        var marker = "<!-- JellyTTV Plugin BEGIN -->";
        if (html.Contains(marker, StringComparison.OrdinalIgnoreCase))
        {
            return html;
        }

        var headEnd = html.IndexOf("</head>", StringComparison.OrdinalIgnoreCase);
        if (headEnd < 0)
        {
            return html;
        }

        return html.Insert(headEnd, JellyTTVScriptManager.GetInjectionHtml());
    }
}
