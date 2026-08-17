using System;
using System.Net.Mime;
using Jellyfin.Plugin.JellyTTV.Services;
using MediaBrowser.Common.Api;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.JellyTTV.Controllers;

/// <summary>
/// API controller that proxies requests from Jellyfin's web UI to the JellyTTV backend.
/// This avoids CORS issues since the web client talks to same-origin endpoints.
/// </summary>
[ApiController]
[Route("JellyTTV")]
public class TwitchController : ControllerBase
{
    private readonly JellyTTVClient _client;

    public TwitchController(JellyTTVClient client)
    {
        _client = client;
    }

    /// <summary>
    /// Gets the list of live Twitch channels from JellyTTV.
    /// </summary>
    [HttpGet("Live")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status503ServiceUnavailable)]
    public async Task<IActionResult> GetLive()
    {
        var data = await _client.GetLiveChannelsAsync().ConfigureAwait(false);
        if (data == null)
        {
            return StatusCode(503, new { error = "JellyTTV backend not reachable" });
        }

        return Ok(data);
    }

    /// <summary>
    /// Proxies a channel thumbnail image from JellyTTV.
    /// </summary>
    [HttpGet("Thumbnail/{channelId}")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<IActionResult> GetThumbnail(int channelId)
    {
        var image = await _client.GetImageAsync(channelId, "thumbnail").ConfigureAwait(false);
        if (image == null)
        {
            return NotFound();
        }

        return File(image.Value.Data, image.Value.ContentType);
    }

    /// <summary>
    /// Proxies a channel avatar image from JellyTTV.
    /// </summary>
    [HttpGet("Avatar/{channelId}")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<IActionResult> GetAvatar(int channelId)
    {
        var image = await _client.GetImageAsync(channelId, "avatar").ConfigureAwait(false);
        if (image == null)
        {
            return NotFound();
        }

        return File(image.Value.Data, image.Value.ContentType);
    }

    /// <summary>
    /// Serves the plugin's JavaScript file as an embedded resource.
    /// </summary>
    [HttpGet("twitch.js")]
    [Produces("application/javascript")]
    public IActionResult GetScript()
    {
        return GetEmbeddedResource("Web.twitch.js", "application/javascript");
    }

    /// <summary>
    /// Serves the plugin's CSS file as an embedded resource.
    /// </summary>
    [HttpGet("twitch.css")]
    [Produces("text/css")]
    public IActionResult GetStyles()
    {
        return GetEmbeddedResource("Web.twitch.css", "text/css");
    }

    private IActionResult GetEmbeddedResource(string resourcePath, string contentType)
    {
        var ns = typeof(Plugin).Namespace;
        var fullpath = $"{ns}.{resourcePath}";
        var assembly = typeof(Plugin).Assembly;
        var stream = assembly.GetManifestResourceStream(fullpath);
        if (stream == null)
        {
            return NotFound($"Resource {fullpath} not found");
        }

        return File(stream, contentType);
    }
}
