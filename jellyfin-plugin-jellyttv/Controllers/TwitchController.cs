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
            enableHomeSection = config?.EnableHomeSection ?? true,
            refreshIntervalSeconds = config?.RefreshIntervalSeconds ?? 60
        });
    }

    /// <summary>
    /// Gets the status of plugin dependencies (Home Screen Sections, Plugin Pages).
    /// </summary>
    [HttpGet("Status")]
    [AllowAnonymous]
    [ProducesResponseType(StatusCodes.Status200OK)]
    public IActionResult GetStatus()
    {
        var hssInstalled = IsAssemblyLoaded(".HomeScreenSections");
        var pluginPagesInstalled = IsAssemblyLoaded(".PluginPages");

        return Ok(new
        {
            homeScreenSectionsInstalled = hssInstalled,
            pluginPagesInstalled = pluginPagesInstalled,
            allDependenciesMet = hssInstalled && pluginPagesInstalled
        });
    }

    private static bool IsAssemblyLoaded(string nameFragment)
    {
        return System.Runtime.Loader.AssemblyLoadContext.All
            .SelectMany(x => x.Assemblies)
            .Any(x => x.FullName?.Contains(nameFragment) ?? false);
    }

    /// <summary>
    /// Tests connectivity to a JellyTTV backend URL from the server side.
    /// Reports staged errors so the user sees the actual failure reason.
    /// </summary>
    [HttpGet("TestConnection")]
    [AllowAnonymous]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType(StatusCodes.Status503ServiceUnavailable)]
    public async Task<IActionResult> TestConnection([FromQuery] string url)
    {
        if (string.IsNullOrWhiteSpace(url))
        {
            return BadRequest(new { success = false, error = "URL is required" });
        }

        var baseUrl = url.TrimEnd('/');

        if (!Uri.TryCreate(baseUrl, UriKind.Absolute, out var uri) ||
            (uri.Scheme != "http" && uri.Scheme != "https"))
        {
            return BadRequest(new { success = false, error = "Invalid URL — must start with http:// or https://" });
        }

        try
        {
            var client = _httpClientFactory.CreateClient("JellyTTV");
            client.Timeout = TimeSpan.FromSeconds(10);

            // Stage 1: reach the health endpoint
            HttpResponseMessage healthResponse;
            try
            {
                healthResponse = await client.GetAsync($"{baseUrl}/api/health").ConfigureAwait(false);
            }
            catch (HttpRequestException ex)
            {
                var reason = ex.InnerException?.Message ?? ex.Message;
                return StatusCode(503, new { success = false, error = $"Cannot reach server: {reason}" });
            }
            catch (TaskCanceledException)
            {
                return StatusCode(503, new { success = false, error = "Connection timed out (10s). Is the URL correct?" });
            }

            if (!healthResponse.IsSuccessStatusCode)
            {
                return StatusCode(503, new { success = false, error = $"Server responded with HTTP {(int)healthResponse.StatusCode} {healthResponse.StatusCode}" });
            }

            // Stage 2: verify the live endpoint returns valid data
            HttpResponseMessage liveResponse;
            try
            {
                liveResponse = await client.GetAsync($"{baseUrl}/api/live").ConfigureAwait(false);
            }
            catch (HttpRequestException ex)
            {
                var reason = ex.InnerException?.Message ?? ex.Message;
                return StatusCode(503, new { success = false, error = $"Health OK but /api/live unreachable: {reason}" });
            }

            if (!liveResponse.IsSuccessStatusCode)
            {
                return StatusCode(503, new { success = false, error = $"Health OK but /api/live returned HTTP {(int)liveResponse.StatusCode}" });
            }

            var liveJson = await liveResponse.Content.ReadAsStringAsync().ConfigureAwait(false);
            if (!liveJson.Contains("\"channels\""))
            {
                return StatusCode(503, new { success = false, error = "Server reachable but /api/live response does not contain expected 'channels' field" });
            }

            return Ok(new { success = true, message = "Connected successfully to JellyTTV" });
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Unexpected error testing connection to {Url}", baseUrl);
            return StatusCode(503, new { success = false, error = $"Unexpected error: {ex.Message}" });
        }
    }

    /// <summary>
    /// Serves the Twitch page HTML for Plugin Pages integration.
    /// </summary>
    [HttpGet("Page")]
    [AllowAnonymous]
    [Produces("text/html")]
    public IActionResult GetPage()
    {
        return GetEmbeddedResource("Web.twitchPage.html", "text/html");
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
