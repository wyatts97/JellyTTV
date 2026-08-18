using System;
using System.Net.Http;
using Jellyfin.Plugin.JellyTTV.Services;
using MediaBrowser.Common.Api;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

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
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<TwitchController> _logger;

    public TwitchController(JellyTTVClient client, IHttpClientFactory httpClientFactory, ILogger<TwitchController> logger)
    {
        _client = client;
        _httpClientFactory = httpClientFactory;
        _logger = logger;
    }

    /// <summary>
    /// Gets the list of live Twitch channels from JellyTTV.
    /// </summary>
    [HttpGet("Live")]
    [AllowAnonymous]
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
    [AllowAnonymous]
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
    [AllowAnonymous]
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
    /// Gets the plugin's display configuration for the client script.
    /// </summary>
    [HttpGet("Config")]
    [AllowAnonymous]
    [ProducesResponseType(StatusCodes.Status200OK)]
    public IActionResult GetConfig()
    {
        var config = Plugin.Instance?.Configuration;
        return Ok(new
        {
            enableSidebarLink = config?.EnableSidebarLink ?? true,
            enableHomeSection = config?.EnableHomeSection ?? true,
            enableNotifications = config?.EnableNotifications ?? true,
            refreshIntervalSeconds = config?.RefreshIntervalSeconds ?? 60
        });
    }

    /// <summary>
    /// Tests connectivity to a JellyTTV backend URL from the server side.
    /// </summary>
    [HttpGet("TestConnection")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status503ServiceUnavailable)]
    public async Task<IActionResult> TestConnection([FromQuery] string url)
    {
        if (string.IsNullOrWhiteSpace(url))
        {
            return BadRequest(new { error = "URL is required" });
        }

        try
        {
            var baseUrl = url.TrimEnd('/');
            var client = _httpClientFactory.CreateClient("JellyTTV");
            client.Timeout = TimeSpan.FromSeconds(10);

            var response = await client.GetAsync($"{baseUrl}/api/live").ConfigureAwait(false);
            if (response.IsSuccessStatusCode)
            {
                return Ok(new { success = true, message = "Connected successfully" });
            }

            return StatusCode(503, new { success = false, error = $"Server responded with status {response.StatusCode}" });
        }
        catch (Exception ex)
        {
            return StatusCode(503, new { success = false, error = ex.Message });
        }
    }

    /// <summary>
    /// Serves the plugin's JavaScript file as an embedded resource.
    /// </summary>
    [HttpGet("twitch.js")]
    [AllowAnonymous]
    [Produces("application/javascript")]
    public IActionResult GetScript()
    {
        return GetEmbeddedResource("Web.twitch.js", "application/javascript");
    }

    /// <summary>
    /// Serves the plugin's CSS file as an embedded resource.
    /// </summary>
    [HttpGet("twitch.css")]
    [AllowAnonymous]
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
