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
public class TwitchResultsHandler
{
    private readonly ILibraryManager _libraryManager;
    private readonly IDtoService _dtoService;
    private readonly IUserManager _userManager;
    private readonly JellyTTVClient _jellyTTVClient;
    private readonly ILogger<TwitchResultsHandler> _logger;

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
    /// Returns live Twitch channels as a QueryResult of BaseItemDto for the HSS section.
    /// </summary>
    public QueryResult<BaseItemDto> GetResults(HomeScreenSectionPayload payload)
    {
        try
        {
            var user = _userManager.GetUserById(payload.UserId);
            if (user == null)
            {
                return new QueryResult<BaseItemDto>();
            }

            // Get live channel display names from the JellyTTV backend
            var liveData = _jellyTTVClient.GetLiveChannelsAsync().GetAwaiter().GetResult();
            if (liveData?.Channels == null || liveData.Channels.Count == 0)
            {
                return new QueryResult<BaseItemDto>();
            }

            var liveNames = new HashSet<string>(
                liveData.Channels.Select(c => c.DisplayName),
                StringComparer.OrdinalIgnoreCase);

            // Query all LiveTvChannel items from Jellyfin's library
            var query = new InternalItemsQuery(user)
            {
                IncludeItemTypes = new[] { BaseItemKind.LiveTvChannel },
                EnableTotalRecordCount = false
            };

            var channels = _libraryManager.GetItemList(query)
                .OfType<LiveTvChannel>()
                .Where(ch => liveNames.Contains(ch.Name))
                .Take(32)
                .ToList();

            if (channels.Count == 0)
            {
                return new QueryResult<BaseItemDto>();
            }

            var dtoOptions = new DtoOptions
            {
                EnableImages = true,
                Fields = new List<ItemFields>
                {
                    ItemFields.PrimaryImageAspectRatio,
                    ItemFields.Path
                }
            };
            dtoOptions.ImageTypeLimit = 1;
            dtoOptions.ImageTypes = new List<ImageType>
            {
                ImageType.Primary,
                ImageType.Thumb,
                ImageType.Backdrop
            };

            var dtos = channels
                .Select(ch => _dtoService.GetBaseItemDto(ch, dtoOptions, user))
                .ToArray();

            return new QueryResult<BaseItemDto>(dtos);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to get Twitch results for HSS section");
            return new QueryResult<BaseItemDto>();
        }
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
