using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
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
            if (attempt > 1)
            {
                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(2 * attempt), cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
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

        JsonNode? root;
        try
        {
            root = string.IsNullOrWhiteSpace(json) ? new JsonObject() : JsonNode.Parse(json);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to parse Plugin Pages config.json");
            return false;
        }

        if (root is not JsonObject rootObj)
        {
            _logger.LogWarning("Plugin Pages config.json root is not a JSON object");
            return false;
        }

        // Get or create pages array
        var pagesArray = rootObj["pages"] as JsonArray;
        if (pagesArray is null)
        {
            if (rootObj["pages"] is not null)
            {
                _logger.LogWarning("Plugin Pages config.json 'pages' is not an array");
                return false;
            }
            pagesArray = new JsonArray();
            rootObj["pages"] = pagesArray;
        }

        // Check if our page is already registered; repair any corrupted string entries
        var found = false;
        for (var i = 0; i < pagesArray.Count; i++)
        {
            var item = pagesArray[i];

            // Repair corrupted string entries from v0.3.0 bug
            if (item is JsonValue val && val.TryGetValue<string>(out var str))
            {
                try
                {
                    var parsed = JsonNode.Parse(str);
                    if (parsed is JsonObject parsedObj)
                    {
                        pagesArray[i] = parsedObj;
                        item = parsedObj;
                        _logger.LogInformation("Repaired corrupted Plugin Pages config entry at index {Index}", i);
                    }
                }
                catch
                {
                    continue;
                }
            }

            if (item is JsonObject obj &&
                obj["Id"]?.GetValue<string>() is string id &&
                string.Equals(id, PageId, StringComparison.OrdinalIgnoreCase))
            {
                found = true;
            }
        }

        if (found)
        {
            // Still write back to persist any repairs
            File.WriteAllText(configPath, root.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
            _logger.LogDebug("JellyTTV page already registered in Plugin Pages config");
            return true;
        }

        // Add our page as a proper JSON object (not a stringified string)
        var ourPage = new JsonObject
        {
            ["Id"] = PageId,
            ["DisplayText"] = "Twitch",
            ["Url"] = "/JellyTTV/Page",
            ["Icon"] = "live_tv"
        };

        pagesArray.Add(ourPage);

        try
        {
            File.WriteAllText(configPath, root.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
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
