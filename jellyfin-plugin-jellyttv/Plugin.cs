using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using Jellyfin.Plugin.JellyTTV.Configuration;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.JellyTTV;

/// <summary>
/// Main plugin class for JellyTTV integration.
/// Injects custom JavaScript into Jellyfin's web UI to add Twitch live streams
/// to the sidebar navigation and home screen.
/// </summary>
public class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    private readonly ILogger<Plugin> _logger;

    /// <summary>
    /// Gets the singleton plugin instance.
    /// </summary>
    public static Plugin? Instance { get; private set; }

    /// <summary>
    /// Initializes a new instance of the <see cref="Plugin"/> class.
    /// </summary>
    public Plugin(
        IApplicationPaths applicationPaths,
        IXmlSerializer xmlSerializer,
        ILogger<Plugin> logger)
        : base(applicationPaths, xmlSerializer)
    {
        _logger = logger;
        Instance = this;

        ConfigurationUpdated += OnConfigurationUpdated;

        TryInjectScript();
    }

    /// <inheritdoc />
    public override string Name => "JellyTTV";

    /// <inheritdoc />
    public override Guid Id => Guid.Parse("a1b2c3d4-e5f6-7890-abcd-ef1234567890");

    /// <inheritdoc />
    public override string Description =>
        "Displays Twitch live streams in Jellyfin's web UI with a dedicated sidebar link and home screen section.";

    /// <inheritdoc />
    public IEnumerable<PluginPageInfo> GetPages()
    {
        var ns = typeof(Plugin).Namespace;
        return new[]
        {
            new PluginPageInfo
            {
                Name = "JellyTTV",
                EmbeddedResourcePath = $"{ns}.Configuration.configPage.html",
                EnableInMainMenu = false
            }
        };
    }

    private void OnConfigurationUpdated(object? sender, PluginConfiguration e)
    {
        _logger.LogInformation("Configuration updated, re-injecting script");
        TryInjectScript();
    }

    /// <summary>
    /// Injects the twitch.js and twitch.css script tags into Jellyfin's index.html.
    /// Idempotent — skips if already injected.
    /// </summary>
    public void TryInjectScript()
    {
        try
        {
            var indexHtmlPath = FindIndexHtml();
            if (indexHtmlPath == null)
            {
                _logger.LogWarning("Could not find Jellyfin index.html; script injection skipped");
                return;
            }

            var html = File.ReadAllText(indexHtmlPath);

            var scriptTag = "<!-- JellyTTV Plugin BEGIN -->";
            if (html.Contains(scriptTag, StringComparison.OrdinalIgnoreCase))
            {
                _logger.LogDebug("Script already injected, skipping");
                return;
            }

            var injection = BuildInjectionHtml();
            var headEnd = html.IndexOf("</head>", StringComparison.OrdinalIgnoreCase);
            if (headEnd < 0)
            {
                _logger.LogWarning("No </head> tag found in index.html; cannot inject");
                return;
            }

            html = html.Insert(headEnd, injection);
            File.WriteAllText(indexHtmlPath, html);
            _logger.LogInformation("Successfully injected JellyTTV script into {Path}", indexHtmlPath);
        }
        catch (UnauthorizedAccessException ex)
        {
            _logger.LogWarning(ex,
                "Cannot write to index.html (permission denied). " +
                "Mount index.html as a volume or install the 'File Transformation' plugin.");
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to inject script into index.html");
        }
    }

    private string? FindIndexHtml()
    {
        var candidates = new[]
        {
            Path.Combine(ApplicationPaths.WebPath, "index.html"),
            "/usr/share/jellyfin/web/index.html",
            "/var/lib/jellyfin/web/index.html",
            "/jellyfin/web/index.html",
            Path.Combine(AppContext.BaseDirectory, "web", "index.html"),
            Path.Combine(Environment.CurrentDirectory, "web", "index.html")
        };

        foreach (var path in candidates)
        {
            try
            {
                if (File.Exists(path))
                {
                    return path;
                }
            }
            catch
            {
                // ignore
            }
        }

        return null;
    }

    private string BuildInjectionHtml()
    {
        var version = Assembly.GetExecutingAssembly().GetName().Version?.ToString() ?? "0.1.0";
        return $@"
<!-- JellyTTV Plugin BEGIN -->
<link rel=""stylesheet"" href=""/JellyTTV/twitch.css?v={version}"" />
<script type=""text/javascript"" src=""/JellyTTV/twitch.js?v={version}""></script>
<!-- JellyTTV Plugin END -->
";
    }
}
