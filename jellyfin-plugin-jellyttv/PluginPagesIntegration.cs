using System;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Common.Configuration;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.JellyTTV;

/// <summary>
/// Registers a "Twitch" page with the Plugin Pages plugin by writing to its config.json.
/// </summary>
public sealed class PluginPagesIntegration : IDisposable
{
    private static readonly string PageId = "jellyttv-twitch-page";
    private readonly ILogger<PluginPagesIntegration> _logger;
    private readonly IApplicationPaths _applicationPaths;
    private readonly CancellationTokenSource _cts = new();
    private bool _disposed;

    public PluginPagesIntegration(IApplicationPaths applicationPaths, ILogger<PluginPagesIntegration> logger)
    {
        _applicationPaths = applicationPaths;
        _logger = logger;
        _ = RegisterAsync(_cts.Token);
    }

    private async Task RegisterAsync(CancellationToken cancellationToken)
    {
        for (var attempt = 1; attempt <= 5; attempt++)
        {
            try
            {
                await Task.Delay(TimeSpan.FromSeconds(2 * attempt), cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }

            try
            {
                if (TryRegister())
                {
                    _logger.LogInformation("JellyTTV registered Twitch page with Plugin Pages on attempt {Attempt}", attempt);
                    return;
                }
            }
            catch (Exception ex)
            {
                _logger.LogDebug(ex, "Plugin Pages registration attempt {Attempt} failed", attempt);
            }
        }

        _logger.LogWarning("JellyTTV could not register with Plugin Pages after 5 attempts. Ensure the Plugin Pages plugin is installed and enabled.");
    }

    private bool TryRegister()
    {
        var configDir = Path.Combine(_applicationPaths.PluginConfigurationsPath, "Jellyfin.Plugin.PluginPages");
        var configPath = Path.Combine(configDir, "config.json");

        if (!File.Exists(configPath))
        {
            _logger.LogDebug("Plugin Pages config.json not found at {Path}", configPath);
            return false;
        }

        var json = File.ReadAllText(configPath);
        using var doc = JsonDocument.Parse(json);

        // Check if our page is already registered
        if (doc.RootElement.TryGetProperty("pages", out var existingPages))
        {
            foreach (var page in existingPages.EnumerateArray())
            {
                if (page.TryGetProperty("Id", out var id) &&
                    string.Equals(id.GetString(), PageId, StringComparison.OrdinalIgnoreCase))
                {
                    _logger.LogDebug("JellyTTV page already registered in Plugin Pages config");
                    return true;
                }
            }
        }

        // Add our page to the existing config
        var pagesArray = doc.RootElement.TryGetProperty("pages", out var pages)
            ? pages.EnumerateArray().Select(p => p.GetRawText()).ToList()
            : new System.Collections.Generic.List<string>();

        var ourPage = new
        {
            Id = PageId,
            DisplayText = "Twitch",
            Url = "/JellyTTV/Page",
            Icon = "live_tv"
        };

        pagesArray.Add(JsonSerializer.Serialize(ourPage));

        var newConfig = new { pages = pagesArray };
        var newJson = JsonSerializer.Serialize(newConfig, new JsonSerializerOptions { WriteIndented = true });

        try
        {
            File.WriteAllText(configPath, newJson);
            _logger.LogInformation("JellyTTV page added to Plugin Pages config.json");
            return true;
        }
        catch (UnauthorizedAccessException ex)
        {
            _logger.LogWarning(ex, "Cannot write to Plugin Pages config.json — insufficient permissions");
            return false;
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        _cts.Cancel();
        _cts.Dispose();
    }
}
