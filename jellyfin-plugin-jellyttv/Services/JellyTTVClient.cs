using System;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using Jellyfin.Plugin.JellyTTV.Configuration;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.JellyTTV.Services;

/// <summary>
/// HTTP client for communicating with the JellyTTV backend API.
/// Caches responses to avoid excessive polling.
/// </summary>
public class JellyTTVClient
{
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<JellyTTVClient> _logger;

    // Property names are pinned with [JsonPropertyName] on the models, so no
    // naming policy is needed. Case-insensitive matching is kept as a safety net.
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true
    };

    // Simple in-memory cache
    private LiveChannelData? _cachedData;
    private DateTime _cacheTime = DateTime.MinValue;
    private readonly object _cacheLock = new();

    public JellyTTVClient(IHttpClientFactory httpClientFactory, ILogger<JellyTTVClient> logger)
    {
        _httpClientFactory = httpClientFactory;
        _logger = logger;
    }

    /// <summary>
    /// Gets the live channel data from JellyTTV, with caching.
    /// </summary>
    public async Task<LiveChannelData?> GetLiveChannelsAsync()
    {
        var config = Plugin.Instance?.Configuration;
        if (config == null || string.IsNullOrWhiteSpace(config.JellyTTVUrl))
        {
            return null;
        }

        var refreshInterval = Math.Max(10, config.RefreshIntervalSeconds);

        lock (_cacheLock)
        {
            if (_cachedData != null && (DateTime.UtcNow - _cacheTime).TotalSeconds < refreshInterval)
            {
                return _cachedData;
            }
        }

        try
        {
            var baseUrl = config.JellyTTVUrl.TrimEnd('/');
            var client = _httpClientFactory.CreateClient("JellyTTV");
            client.Timeout = TimeSpan.FromSeconds(10);

            var url = $"{baseUrl}/api/live";
            var response = await client.GetAsync(url).ConfigureAwait(false);
            response.EnsureSuccessStatusCode();

            var json = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            var data = JsonSerializer.Deserialize<LiveChannelData>(json, JsonOptions);

            if (data == null)
            {
                return null;
            }

            // Contract-drift guard: if the backend payload changed shape and every
            // channel deserialized to defaults, log a warning so the operator can
            // investigate instead of silently seeing an empty live list.
            if (data.Channels is { Count: > 0 } channels)
            {
                var allDefaults = channels.TrueForAll(c =>
                    c.Id == 0 && string.IsNullOrEmpty(c.Login) && !c.IsLive);
                if (allDefaults)
                {
                    _logger.LogWarning(
                        "JellyTTV returned {Count} channels but all deserialized to default values — " +
                        "JSON contract may have changed. Raw payload: {Payload}",
                        channels.Count, json.Length > 500 ? json[..500] + "..." : json);
                }
            }

            // Always rewrite thumbnail and avatar URLs to go through our proxy
            // controller. The backend may return relative URLs (/api/channels/...)
            // or absolute Twitch CDN URLs — both need to be proxied through Jellyfin
            // to avoid CORS issues and broken images in the web client.
            var pluginBase = "/JellyTTV";
            if (data.Channels != null)
            {
                foreach (var ch in data.Channels)
                {
                    ch.ThumbnailUrl = $"{pluginBase}/Thumbnail/{ch.Id}";
                    ch.AvatarUrl = $"{pluginBase}/Avatar/{ch.Id}";
                }

                _logger.LogInformation("JellyTTV live data: {Count} channels live", data.Channels.Count);
            }

            lock (_cacheLock)
            {
                _cachedData = data;
                _cacheTime = DateTime.UtcNow;
            }

            return data;
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to fetch live channels from JellyTTV at {Url}", config.JellyTTVUrl);
            return _cachedData; // return stale cache if available
        }
    }

    /// <summary>
    /// Fetches a raw image (thumbnail or avatar) from JellyTTV's proxy endpoint.
    /// </summary>
    public async Task<(byte[] Data, string ContentType)?> GetImageAsync(int channelId, string imageType)
    {
        var config = Plugin.Instance?.Configuration;
        if (config == null || string.IsNullOrWhiteSpace(config.JellyTTVUrl))
        {
            return null;
        }

        try
        {
            var baseUrl = config.JellyTTVUrl.TrimEnd('/');
            var client = _httpClientFactory.CreateClient("JellyTTV");
            client.Timeout = TimeSpan.FromSeconds(10);

            var url = imageType == "avatar"
                ? $"{baseUrl}/api/channels/{channelId}/avatar"
                : $"{baseUrl}/api/channels/{channelId}/thumbnail";

            var response = await client.GetAsync(url).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                return null;
            }

            var data = await response.Content.ReadAsByteArrayAsync().ConfigureAwait(false);
            var contentType = response.Content.Headers.ContentType?.MediaType ?? "image/jpeg";
            return (data, contentType);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to fetch {Type} for channel {Id}", imageType, channelId);
            return null;
        }
    }
}

/// <summary>
/// Response model matching JellyTTV's /api/live endpoint.
/// </summary>
/// <remarks>
/// Every property is pinned with <see cref="JsonPropertyNameAttribute"/>. This matters in
/// both directions: the JellyTTV backend emits snake_case, and Jellyfin serialises plugin
/// controller responses with a PascalCase policy. Without these attributes the payload we
/// hand to the web client would be renamed and every field would read as undefined there.
/// </remarks>
public class LiveChannelData
{
    /// <summary>Gets or sets the list of currently live channels.</summary>
    [JsonPropertyName("channels")]
    public System.Collections.Generic.List<LiveChannelInfo>? Channels { get; set; }
}

/// <summary>
/// Individual live channel info as returned by JellyTTV.
/// </summary>
public class LiveChannelInfo
{
    /// <summary>Gets or sets the JellyTTV channel id.</summary>
    [JsonPropertyName("id")]
    public int Id { get; set; }

    /// <summary>Gets or sets the Twitch login (url slug).</summary>
    [JsonPropertyName("login")]
    public string Login { get; set; } = string.Empty;

    /// <summary>Gets or sets the Twitch display name.</summary>
    [JsonPropertyName("display_name")]
    public string DisplayName { get; set; } = string.Empty;

    /// <summary>Gets or sets a value indicating whether the channel is live.</summary>
    [JsonPropertyName("is_live")]
    public bool IsLive { get; set; }

    /// <summary>Gets or sets the stream title.</summary>
    [JsonPropertyName("title")]
    public string? Title { get; set; }

    /// <summary>Gets or sets the Twitch category / game name.</summary>
    [JsonPropertyName("game_name")]
    public string? GameName { get; set; }

    /// <summary>Gets or sets the current viewer count.</summary>
    [JsonPropertyName("viewer_count")]
    public int ViewerCount { get; set; }

    /// <summary>Gets or sets the live preview url, proxied through this plugin.</summary>
    [JsonPropertyName("thumbnail_url")]
    public string? ThumbnailUrl { get; set; }

    /// <summary>Gets or sets the streamer avatar url, proxied through this plugin.</summary>
    [JsonPropertyName("avatar_url")]
    public string? AvatarUrl { get; set; }

    /// <summary>Gets or sets the ISO-8601 timestamp the stream started at.</summary>
    [JsonPropertyName("started_at")]
    public string? StartedAt { get; set; }
}
