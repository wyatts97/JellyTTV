using Jellyfin.Plugin.JellyTTV.Services;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Plugins;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.JellyTTV;

/// <summary>
/// Registers plugin services with Jellyfin's DI container.
/// </summary>
public class ServiceRegistrator : IPluginServiceRegistrator
{
    /// <inheritdoc />
    public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
    {
        serviceCollection.AddHttpClient("JellyTTV");
        serviceCollection.AddSingleton<JellyTTVClient>();
        serviceCollection.AddSingleton<HomeScreenSectionsIntegration>();
        serviceCollection.AddSingleton<PluginPagesIntegration>();
        serviceCollection.AddSingleton<LiveTvGuideRefresher>();
    }
}
