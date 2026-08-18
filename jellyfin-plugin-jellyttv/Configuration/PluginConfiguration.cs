using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.JellyTTV.Configuration;

/// <summary>
/// Plugin configuration for JellyTTV integration.
/// </summary>
public class PluginConfiguration : BasePluginConfiguration
{
    /// <summary>
    /// Gets or sets the base URL of the JellyTTV backend (e.g. http://jellyttv-api:8730).
    /// </summary>
    public string JellyTTVUrl { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets a value indicating whether the "Live on Twitch" home screen section is shown.
    /// </summary>
    public bool EnableHomeSection { get; set; } = true;

    /// <summary>
    /// Gets or sets the refresh interval in seconds for polling live data.
    /// </summary>
    public int RefreshIntervalSeconds { get; set; } = 60;
}
