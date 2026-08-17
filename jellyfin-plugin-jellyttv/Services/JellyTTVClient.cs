using System;
using System.Net.Http;
using System.Text.Json;
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

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
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

            // Rewrite thumbnail and avatar URLs to go through our proxy controller
            var pluginBase = "/JellyTTV";
            if (data.Channels != null)
            {
                foreach (var ch in data.Channels)
                {
                    if (!string.IsNullOrEmpty(ch.ThumbnailUrl))
                    {
                        // Keep relative proxy URLs from JellyTTV as-is, rewrite absolute Twitch URLs
                        if (ch.ThumbnailUrl.StartsWith("http", StringComparison.OrdinalIgnoreCase))
                        {
                            ch.ThumbnailUrl = $"{pluginBase}/Thumbnail/{ch.Id}";
                        }
                    }

                    if (!string.IsNullOrEmpty(ch.AvatarUrl))
                    {
                        if (ch.AvatarUrl.StartsWith("http", StringComparison.OrdinalIgnoreCase))
                        {
                            ch.AvatarUrl = $"{pluginBase}/Avatar/{ch.Id}";
                        }
                    }
                }
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
/// Response model matching JellyTTV's /api/system endpoint.
/// </summary>
public class LiveChannelData
{
    public System.Collections.Generic.List<LiveChannelInfo>? Channels { get; set; }
}

/// <summary>
/// Individual live channel info as returned by JellyTTV.
/// </summary>
public class LiveChannelInfo
{
    public int Id { get; set; }
    public string Login { get; set; } = string.Empty;
    public string DisplayName { get; set; } = string.Empty;
    public bool IsLive { get; set; }
    public string? Title { get; set; }
    public string? GameName { get; set; }
    public int ViewerCount { get; set; }
    public string? ThumbnailUrl { get; set; }
    public string? AvatarUrl { get; set; }
    public string? StartedAt { get; set; }
}
