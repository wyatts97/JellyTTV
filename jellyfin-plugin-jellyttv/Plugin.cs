using System;
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
public class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages, IDisposable
{
    private readonly JellyTTVScriptManager _scriptManager;
    private bool _disposed;

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
        ILogger<Plugin> logger,
        JellyTTVScriptManager scriptManager)
        : base(applicationPaths, xmlSerializer)
    {
        Instance = this;
        _scriptManager = scriptManager;
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

    /// <inheritdoc />
    public override void OnUninstalling()
    {
        Dispose();
        base.OnUninstalling();
    }

    /// <inheritdoc />
    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        _scriptManager.Dispose();
        Instance = null;
    }
}
