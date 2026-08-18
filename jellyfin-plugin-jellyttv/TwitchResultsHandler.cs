using System;
using System.Collections.Generic;
using System.Linq;
using Jellyfin.Data.Enums;
using Jellyfin.Plugin.JellyTTV.Services;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.Querying;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.JellyTTV;

/// <summary>
/// Results handler invoked by the Home Screen Sections plugin to populate
/// the "Live on Twitch" section. HSS creates instances via DI (ActivatorUtilities).
/// </summary>
public class TwitchResultsHandler
{
    private readonly JellyTTVClient _jellyTTVClient;
    private readonly ILogger<TwitchResultsHandler> _logger;

    public TwitchResultsHandler(
        IUserManager userManager,
        JellyTTVClient jellyTTVClient,
        ILogger<TwitchResultsHandler> logger)
    {
        _jellyTTVClient = jellyTTVClient;
        _logger = logger;
    }

    /// <summary>
    /// Returns live Twitch channels as a QueryResult of BaseItemDto for the HSS section.
    /// Builds synthetic DTOs directly from backend data — does not depend on
    /// LiveTvChannel items existing in Jellyfin's library.
    /// </summary>
    public QueryResult<BaseItemDto> GetResults(HomeScreenSectionPayload payload)
    {
        try
        {
            var liveData = _jellyTTVClient.GetLiveChannelsAsync().GetAwaiter().GetResult();
            if (liveData?.Channels == null || liveData.Channels.Count == 0)
            {
                _logger.LogDebug("No live channels from backend for HSS section");
                return new QueryResult<BaseItemDto>();
            }

            var dtos = liveData.Channels
                .Take(32)
                .Select(ch => BuildDto(ch))
                .ToArray();

            _logger.LogDebug("HSS returning {Count} live Twitch channels", dtos.Length);
            return new QueryResult<BaseItemDto>(dtos);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to get Twitch results for HSS section");
            return new QueryResult<BaseItemDto>();
        }
    }

    private static BaseItemDto BuildDto(LiveChannelInfo ch)
    {
        var dto = new BaseItemDto
        {
            Id = Guid.NewGuid(),
            Name = ch.DisplayName,
            Overview = ch.Title,
            Type = BaseItemKind.LiveTvChannel,
            SortName = ch.DisplayName,
            ForcedSortName = ch.DisplayName,
            DateCreated = DateTime.UtcNow,
            PremiereDate = ch.StartedAt != null
                ? DateTimeOffset.TryParse(ch.StartedAt, out var started)
                    ? started.UtcDateTime
                    : (DateTime?)null
                : null,
            Genres = string.IsNullOrEmpty(ch.GameName)
                ? Array.Empty<string>()
                : new[] { ch.GameName },
            Path = $"https://twitch.tv/{ch.Login}",
            Taglines = new[] { $"LIVE • {ch.ViewerCount:N0} viewers" },
            ImageTags = new Dictionary<ImageType, string>
            {
                { ImageType.Primary, ch.ThumbnailUrl ?? ch.AvatarUrl ?? string.Empty },
                { ImageType.Thumb, ch.ThumbnailUrl ?? string.Empty },
                { ImageType.Logo, ch.AvatarUrl ?? string.Empty }
            },
            BackdropImageTags = new[] { ch.ThumbnailUrl ?? string.Empty },
            ParentBackdropImageTags = Array.Empty<string>(),
            ProviderIds = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                { "Twitch", ch.Login }
            }
        };

        return dto;
    }
}

/// <summary>
/// Payload received from the Home Screen Sections plugin when requesting section results.
/// </summary>
public class HomeScreenSectionPayload
{
    public Guid UserId { get; set; }
    public string? AdditionalData { get; set; }
}
