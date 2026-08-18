using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Jellyfin.Plugin.JellyTTV.Services;
using MediaBrowser.Controller;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.JellyTTV;

/// <summary>
/// Background service that polls the JellyTTV backend for live channel changes
/// and triggers Jellyfin's "Refresh Guide" scheduled task when the set of live
/// channels changes, so Live TV guide data stays current without manual refresh.
/// </summary>
public sealed class LiveTvGuideRefresher : IDisposable
{
    private readonly JellyTTVClient _client;
    private readonly IServerApplicationHost _appHost;
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<LiveTvGuideRefresher> _logger;
    private readonly CancellationTokenSource _cts = new();
    private HashSet<int> _lastLiveIds = new();
    private bool _disposed;

    public LiveTvGuideRefresher(
        JellyTTVClient client,
        IServerApplicationHost appHost,
        IHttpClientFactory httpClientFactory,
        ILogger<LiveTvGuideRefresher> logger)
    {
        _client = client;
        _appHost = appHost;
        _httpClientFactory = httpClientFactory;
        _logger = logger;
        _ = PollLoop(_cts.Token);
    }

    private async Task PollLoop(CancellationToken cancellationToken)
    {
        // Wait a bit for server startup to settle
        await Task.Delay(TimeSpan.FromSeconds(15), cancellationToken).ConfigureAwait(false);

        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                var data = await _client.GetLiveChannelsAsync().ConfigureAwait(false);
                if (data?.Channels != null)
                {
                    var currentLiveIds = data.Channels
                        .Where(c => c.IsLive)
                        .Select(c => c.Id)
                        .ToHashSet();

                    if (!_lastLiveIds.SetEquals(currentLiveIds))
                    {
                        var added = currentLiveIds.Except(_lastLiveIds).ToList();
                        var removed = _lastLiveIds.Except(currentLiveIds).ToList();

                        if (added.Count > 0)
                        {
                            _logger.LogInformation("Live channels changed: {Added} went live, {Removed} went offline — triggering Jellyfin guide refresh",
                                string.Join(",", added), string.Join(",", removed));
                        }
                        else if (removed.Count > 0 && _lastLiveIds.Count > 0)
                        {
                            _logger.LogInformation("Live channels changed: {Removed} went offline — triggering Jellyfin guide refresh",
                                string.Join(",", removed));
                        }

                        _lastLiveIds = currentLiveIds;
                        await TriggerGuideRefresh().ConfigureAwait(false);
                    }
                    else
                    {
                        _logger.LogDebug("No live channel changes detected");
                    }
                }
            }
            catch (OperationCanceledException)
            {
                return;
            }
            catch (Exception ex)
            {
                _logger.LogDebug(ex, "Error during guide refresher poll");
            }

            var config = Plugin.Instance?.Configuration;
            var interval = Math.Max(30, config?.RefreshIntervalSeconds ?? 60);

            try
            {
                await Task.Delay(TimeSpan.FromSeconds(interval), cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }
    }

    private async Task TriggerGuideRefresh()
    {
        try
        {
            // Get the server's own URL
            var serverUrl = _appHost.ListenWithHttps
                ? $"https://localhost:{_appHost.HttpsPort}"
                : $"http://localhost:{_appHost.HttpPort}";

            var client = _httpClientFactory.CreateClient("JellyTTV");
            client.Timeout = TimeSpan.FromSeconds(10);

            // Trigger the "Refresh Guide" scheduled task via Jellyfin's internal API
            var response = await client.PostAsync($"{serverUrl}/ScheduledTasks/Running/RefreshGuide", null).ConfigureAwait(false);

            if (response.IsSuccessStatusCode)
            {
                _logger.LogInformation("Jellyfin guide refresh triggered successfully");
            }
            else
            {
                _logger.LogWarning("Guide refresh returned HTTP {Status}", (int)response.StatusCode);
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to trigger Jellyfin guide refresh");
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
