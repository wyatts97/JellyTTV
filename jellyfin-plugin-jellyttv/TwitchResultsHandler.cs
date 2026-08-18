using System;
using System.Collections.Generic;
using System.Linq;
using Jellyfin.Data.Enums;
using Jellyfin.Plugin.JellyTTV.Services;
using MediaBrowser.Controller.Dto;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.LiveTv;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.Querying;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.JellyTTV;

/// <summary>
/// Results handler invoked by the Home Screen Sections plugin to populate
/// the "Live on Twitch" section. HSS creates instances via DI (ActivatorUtilities).
/// </summary>
/// <remarks>
/// This deliberately returns <b>real</b> <c>LiveTvChannel</c> items from Jellyfin's library
/// rather than synthetic ones. Home Screen Sections offers no supported way for a third-party
/// plugin to attach an external image url to a card — <c>ImageTags</c> values must be real
/// image hashes, and HSS's own Jellyseerr sections only render remote posters because its
/// bundled client script special-cases them. Synthetic items also carry ids that resolve to
/// nothing, which makes the web client's card builder throw mid-render and collapses the whole
/// modular home layout. Using genuine library items gives us working artwork (sourced from the
/// playlist's <c>tvg-logo</c>), in-app playback and favourites for free.
/// </remarks>
public class TwitchResultsHandler
{
    private const int MaxItems = 32;

    private readonly ILibraryManager _libraryManager;
    private readonly IDtoService _dtoService;
    private readonly IUserManager _userManager;
    private readonly JellyTTVClient _jellyTTVClient;
    private readonly ILogger<TwitchResultsHandler> _logger;

    /// <summary>
    /// Initializes a new instance of the <see cref="TwitchResultsHandler"/> class.
    /// </summary>
    public TwitchResultsHandler(
        ILibraryManager libraryManager,
        IDtoService dtoService,
        IUserManager userManager,
        JellyTTVClient jellyTTVClient,
        ILogger<TwitchResultsHandler> logger)
    {
        _libraryManager = libraryManager;
        _dtoService = dtoService;
        _userManager = userManager;
        _jellyTTVClient = jellyTTVClient;
        _logger = logger;
    }

    /// <summary>
    /// Returns the Live TV channels of currently-live Twitch streamers.
    /// </summary>
    /// <remarks>
    /// Never throws. Home Screen Sections invokes this inline while building the home screen,
    /// so an escaping exception would surface as an HTTP 500 and break the entire page.
    /// </remarks>
    public QueryResult<BaseItemDto> GetResults(HomeScreenSectionPayload payload)
    {
        try
        {
            var liveData = _jellyTTVClient.GetLiveChannelsAsync().GetAwaiter().GetResult();
            var liveChannels = liveData?.Channels;
            if (liveChannels == null || liveChannels.Count == 0)
            {
                _logger.LogDebug("JellyTTV reports nobody live; 'Live on Twitch' section is empty");
                return Empty();
            }

            var user = _userManager.GetUserById(payload.UserId);
            if (user == null)
            {
                _logger.LogWarning("HSS supplied unknown user id {UserId}", payload.UserId);
                return Empty();
            }

            // Match on login as well as display name. Jellyfin names channels from the
            // playlist's tvg-name (the Twitch display name), but a streamer can change
            // that at any time while the login stays fixed, and matching on display name
            // alone silently emptied the section whenever the guide was stale.
            var wanted = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var channel in liveChannels)
            {
                if (!string.IsNullOrWhiteSpace(channel.DisplayName))
                {
                    wanted.Add(channel.DisplayName);
                }

                if (!string.IsNullOrWhiteSpace(channel.Login))
                {
                    wanted.Add(channel.Login);
                }
            }

            var allChannels = _libraryManager.GetItemList(new InternalItemsQuery(user)
            {
                IncludeItemTypes = new[] { BaseItemKind.LiveTvChannel },
                EnableTotalRecordCount = false
            }).OfType<LiveTvChannel>().ToList();

            var matched = allChannels
                .Where(ch => wanted.Contains(ch.Name))
                .Take(MaxItems)
                .ToList();

            if (matched.Count == 0)
            {
                _logger.LogWarning(
                    "'Live on Twitch' is empty: {LiveCount} channel(s) live on Twitch ({LiveNames}) " +
                    "but none matched any of the {ChannelCount} Jellyfin Live TV channel(s). " +
                    "Check that the JellyTTV tuner is added in Jellyfin and its guide has refreshed.",
                    liveChannels.Count,
                    string.Join(", ", liveChannels.Select(c => c.DisplayName)),
                    allChannels.Count);
                return Empty();
            }

            // Preserve the backend's ordering so the section is stable between refreshes.
            // Assigned via the indexer rather than ToDictionary: two channels sharing a
            // display name is unlikely but must not throw here.
            var order = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            for (var i = 0; i < liveChannels.Count; i++)
            {
                var name = liveChannels[i].DisplayName;
                if (!string.IsNullOrWhiteSpace(name))
                {
                    order[name] = i;
                }
            }

            var dtoOptions = new DtoOptions
            {
                EnableImages = true,
                Fields = new List<ItemFields>
                {
                    ItemFields.PrimaryImageAspectRatio,
                    ItemFields.Path
                },
                ImageTypeLimit = 1,
                ImageTypes = new List<ImageType>
                {
                    ImageType.Primary,
                    ImageType.Thumb,
                    ImageType.Backdrop
                }
            };

            var dtos = matched
                .OrderBy(ch => order.TryGetValue(ch.Name, out var index) ? index : int.MaxValue)
                .Select(ch => _dtoService.GetBaseItemDto(ch, dtoOptions, user))
                .ToArray();

            _logger.LogInformation(
                "'Live on Twitch' matched {Matched} of {LiveCount} live channel(s) to Jellyfin Live TV",
                dtos.Length,
                liveChannels.Count);

            return new QueryResult<BaseItemDto>(dtos);
        }
        catch (Exception ex)
        {
            // Swallow deliberately: returning an empty section degrades gracefully,
            // whereas letting this escape takes the whole home screen down with it.
            _logger.LogError(ex, "Failed to build the 'Live on Twitch' section");
            return Empty();
        }
    }

    private static QueryResult<BaseItemDto> Empty() => new(Array.Empty<BaseItemDto>());
}

/// <summary>
/// Payload received from the Home Screen Sections plugin when requesting section results.
/// </summary>
public class HomeScreenSectionPayload
{
    public Guid UserId { get; set; }
    public string? AdditionalData { get; set; }
}
