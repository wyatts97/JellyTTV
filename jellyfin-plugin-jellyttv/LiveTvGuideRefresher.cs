using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Jellyfin.Plugin.JellyTTV.Services;
using MediaBrowser.Model.Tasks;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.JellyTTV;

/// <summary>
/// Background service that polls the JellyTTV backend for live channel changes and
/// triggers Jellyfin's "Refresh Guide" scheduled task whenever the set of live channels
/// changes, so Live TV reflects go-live/go-offline events without a manual guide refresh.
/// </summary>
/// <remarks>
/// The task is executed in-process through <see cref="ITaskManager"/> rather than by POSTing
/// to <c>/ScheduledTasks/Running/RefreshGuide</c>. That endpoint requires an admin token, so
/// the HTTP approach returned 401; going through the task manager needs no credentials and is
/// unaffected by bind address, HTTPS or a reverse proxy.
/// </remarks>
public sealed class LiveTvGuideRefresher : IDisposable
{
    private const string RefreshGuideTaskKey = "RefreshGuide";

    private readonly JellyTTVClient _client;
    private readonly ITaskManager _taskManager;
    private readonly ILogger<LiveTvGuideRefresher> _logger;
    private readonly CancellationTokenSource _cts = new();
    private HashSet<int>? _lastLiveIds;
    private bool _warnedTaskMissing;
    private bool _disposed;

    /// <summary>
    /// Initializes a new instance of the <see cref="LiveTvGuideRefresher"/> class.
    /// </summary>
    public LiveTvGuideRefresher(
        JellyTTVClient client,
        ITaskManager taskManager,
        ILogger<LiveTvGuideRefresher> logger)
    {
        _client = client;
        _taskManager = taskManager;
        _logger = logger;
        _ = PollLoop(_cts.Token);
    }

    private async Task PollLoop(CancellationToken cancellationToken)
    {
        try
        {
            // Let server startup settle before the first backend call.
            await Task.Delay(TimeSpan.FromSeconds(15), cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            return;
        }

        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                await PollOnce().ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            catch (Exception ex)
            {
                _logger.LogDebug(ex, "Error during guide refresher poll");
            }

            var interval = Math.Max(30, Plugin.Instance?.Configuration.RefreshIntervalSeconds ?? 60);

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

    private async Task PollOnce()
    {
        var data = await _client.GetLiveChannelsAsync().ConfigureAwait(false);
        if (data?.Channels == null)
        {
            return;
        }

        var currentLiveIds = data.Channels
            .Where(c => c.IsLive)
            .Select(c => c.Id)
            .ToHashSet();

        // First successful poll only establishes a baseline. Refreshing here would fire a
        // redundant guide rebuild on every server restart.
        if (_lastLiveIds == null)
        {
            _lastLiveIds = currentLiveIds;
            _logger.LogDebug("Guide refresher baseline: {Count} channel(s) live", currentLiveIds.Count);
            return;
        }

        if (_lastLiveIds.SetEquals(currentLiveIds))
        {
            return;
        }

        var wentLive = currentLiveIds.Except(_lastLiveIds).ToList();
        var wentOffline = _lastLiveIds.Except(currentLiveIds).ToList();
        _lastLiveIds = currentLiveIds;

        _logger.LogInformation(
            "Live channels changed ({LiveCount} now live: +[{WentLive}] -[{WentOffline}]) — refreshing the Jellyfin guide",
            currentLiveIds.Count,
            string.Join(",", wentLive),
            string.Join(",", wentOffline));

        TriggerGuideRefresh();
    }

    private void TriggerGuideRefresh()
    {
        var worker = _taskManager.ScheduledTasks
            .FirstOrDefault(t => string.Equals(t.ScheduledTask.Key, RefreshGuideTaskKey, StringComparison.OrdinalIgnoreCase));

        if (worker == null)
        {
            if (!_warnedTaskMissing)
            {
                _warnedTaskMissing = true;
                _logger.LogWarning(
                    "Jellyfin's '{Key}' scheduled task was not found, so the Live TV guide cannot be " +
                    "refreshed automatically. This is expected if Live TV is not configured.",
                    RefreshGuideTaskKey);
            }

            return;
        }

        if (worker.State == TaskState.Running)
        {
            _logger.LogDebug("Guide refresh already running; skipping");
            return;
        }

        try
        {
            _taskManager.Execute(worker, new TaskOptions());
            _logger.LogInformation("Triggered Jellyfin '{Key}' task", RefreshGuideTaskKey);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to trigger the Jellyfin guide refresh");
        }
    }

    /// <inheritdoc />
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
