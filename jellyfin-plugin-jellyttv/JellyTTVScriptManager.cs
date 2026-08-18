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
            _logger.LogWarning(ex, "JavaScript Injector registration failed");
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
            _logger.LogWarning(ex, "File Transformation registration failed");
        }

        _logger.LogWarning("No external injection plugin found; using built-in middleware");
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
            .FirstOrDefault(x => string.Equals(x.GetName().Name, "Jellyfin.Plugin.JavaScriptInjector", StringComparison.Ordinal));

        if (assembly == null)
        {
            _logger.LogDebug("JavaScript Injector assembly not found");
            return false;
        }

        _logger.LogDebug("Found JavaScript Injector assembly: {Assembly}", assembly.FullName);

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

        // Check if the method accepts a JObject or a string parameter.
        var parameters = method.GetParameters();
        if (parameters.Length == 1 && parameters[0].ParameterType == typeof(string))
        {
            // String overload - serialize our JObject to string.
            try
            {
                var json = registration.ToString();
                var result = method.Invoke(null, new object[] { json });
                if (result is true)
                {
                    _logger.LogDebug("JavaScript Injector RegisterScript(string) returned true");
                    return true;
                }
                else
                {
                    _logger.LogWarning("JavaScript Injector RegisterScript(string) returned {Result}", result);
                    return false;
                }
            }
            catch (TargetInvocationException ex) when (ex.InnerException is InvalidOperationException)
            {
                _logger.LogDebug("JavaScript Injector not ready yet: {Message}", ex.InnerException.Message);
                return false;
            }
        }
        else
        {
            // JObject overload - find Newtonsoft.Json in the same AssemblyLoadContext as the target plugin.
            var parseMethod = GetTargetJObjectParseMethod(assembly);
            if (parseMethod == null)
            {
                _logger.LogWarning("Could not find JObject.Parse in target plugin's AssemblyLoadContext");
                return false;
            }

            try
            {
                var json = registration.ToString();
                var targetJObject = parseMethod.Invoke(null, new object[] { json });
                var result = method.Invoke(null, new[] { targetJObject });
                if (result is true)
                {
                    _logger.LogDebug("JavaScript Injector RegisterScript(JObject) returned true");
                    return true;
                }
                else
                {
                    _logger.LogWarning("JavaScript Injector RegisterScript(JObject) returned {Result}", result);
                    return false;
                }
            }
            catch (TargetInvocationException ex) when (ex.InnerException is InvalidOperationException)
            {
                _logger.LogDebug("JavaScript Injector not ready yet: {Message}", ex.InnerException.Message);
                return false;
            }
        }

        // Should not reach here - both branches above return.
        return false;
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

        // Find Newtonsoft.Json in the same AssemblyLoadContext as the target plugin.
        var parseMethod = GetTargetJObjectParseMethod(assembly);
        if (parseMethod == null)
        {
            _logger.LogWarning("Could not find JObject.Parse in target plugin's AssemblyLoadContext");
            return false;
        }

        try
        {
            var json = payload.ToString();
            var targetJObject = parseMethod.Invoke(null, new object[] { json });
            var result = method.Invoke(null, new[] { targetJObject });
            if (result is true)
            {
                _logger.LogDebug("File Transformation RegisterTransformation returned true");
                return true;
            }
            else
            {
                _logger.LogWarning("File Transformation RegisterTransformation returned {Result}", result);
                return false;
            }
        }
        catch (TargetInvocationException ex) when (ex.InnerException is InvalidOperationException)
        {
            _logger.LogDebug("File Transformation not ready yet: {Message}", ex.InnerException.Message);
            return false;
        }
    }

    /// <summary>
    /// Finds the JObject.Parse method from the Newtonsoft.Json assembly loaded in the
    /// same AssemblyLoadContext as the target plugin, to avoid cross-context type mismatch.
    /// </summary>
    private MethodInfo? GetTargetJObjectParseMethod(Assembly targetAssembly)
    {
        // Get the AssemblyLoadContext of the target plugin.
        var targetContext = AssemblyLoadContext.GetLoadContext(targetAssembly);

        // Search all assemblies in that context for Newtonsoft.Json.
        var assemblies = (targetContext != null
            ? targetContext.Assemblies
            : AssemblyLoadContext.All.SelectMany(x => x.Assemblies));

        foreach (var asm in assemblies)
        {
            if (asm.GetName().Name != "Newtonsoft.Json")
            {
                continue;
            }

            var jObjectType = asm.GetType("Newtonsoft.Json.Linq.JObject");
            var parseMethod = jObjectType?.GetMethod("Parse", BindingFlags.Public | BindingFlags.Static, new[] { typeof(string) });
            if (parseMethod != null)
            {
                _logger.LogDebug("Found Newtonsoft.Json JObject.Parse in {Assembly}", asm.FullName);
                return parseMethod;
            }
        }

        // Fallback: search all contexts if not found in the target context.
        foreach (var asm in AssemblyLoadContext.All.SelectMany(x => x.Assemblies))
        {
            if (asm.GetName().Name != "Newtonsoft.Json")
            {
                continue;
            }

            var jObjectType = asm.GetType("Newtonsoft.Json.Linq.JObject");
            var parseMethod = jObjectType?.GetMethod("Parse", BindingFlags.Public | BindingFlags.Static, new[] { typeof(string) });
            if (parseMethod != null)
            {
                _logger.LogDebug("Found Newtonsoft.Json JObject.Parse in fallback {Assembly}", asm.FullName);
                return parseMethod;
            }
        }

        return null;
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
