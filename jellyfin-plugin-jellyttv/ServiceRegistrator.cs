using Jellyfin.Plugin.JellyTTV.Services;
using MediaBrowser.Common;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.JellyTTV;

/// <summary>
/// Registers plugin services with Jellyfin's DI container.
/// </summary>
public class ServiceRegistrator : IPluginServiceRegistrator
{
    /// <inheritdoc />
    public void RegisterServices(IServiceCollection serviceCollection, IApplicationHost applicationHost)
    {
        serviceCollection.AddHttpClient("JellyTTV");
        serviceCollection.AddSingleton<JellyTTVClient>();
    }
}
