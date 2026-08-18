using System;
using System.Linq;
using System.Reflection;
using System.Runtime.Loader;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Extensions.Logging;
using Newtonsoft.Json.Linq;

namespace Jellyfin.Plugin.JellyTTV;

/// <summary>
/// Registers a "Live on Twitch" home screen section with the Home Screen Sections plugin
/// via reflection (to avoid AssemblyLoadContext type mismatches).
/// </summary>
public sealed class HomeScreenSectionsIntegration : IDisposable
{
    private static readonly Guid SectionId = Guid.Parse("b2c3d4e5-f6a7-8901-bcde-f12345678901");

    private readonly ILogger<HomeScreenSectionsIntegration> _logger;
    private readonly CancellationTokenSource _cts = new();
    private bool _disposed;

    public HomeScreenSectionsIntegration(ILogger<HomeScreenSectionsIntegration> logger)
    {
        _logger = logger;
        _ = RegisterAsync(_cts.Token);
    }

    private async Task RegisterAsync(CancellationToken cancellationToken)
    {
        for (var attempt = 1; attempt <= 10; attempt++)
        {
            try
            {
                await Task.Delay(TimeSpan.FromSeconds(Math.Min(2 * attempt, 10)), cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }

            try
            {
                if (TryRegister())
                {
                    _logger.LogInformation("JellyTTV registered 'Live on Twitch' section with Home Screen Sections on attempt {Attempt}", attempt);
                    return;
                }
            }
            catch (Exception ex)
            {
                _logger.LogDebug(ex, "HSS registration attempt {Attempt} failed", attempt);
            }
        }

        _logger.LogWarning("JellyTTV could not register with Home Screen Sections after 10 attempts. Ensure the HSS plugin is installed and enabled.");
    }

    private bool TryRegister()
    {
        var assembly = AssemblyLoadContext.All
            .SelectMany(x => x.Assemblies)
            .FirstOrDefault(x => x.FullName?.Contains(".HomeScreenSections") ?? false);

        if (assembly == null)
        {
            return false;
        }

        _logger.LogDebug("Found Home Screen Sections assembly: {Assembly}", assembly.FullName);

        var pluginInterfaceType = assembly.GetType("Jellyfin.Plugin.HomeScreenSections.PluginInterface");
        if (pluginInterfaceType == null)
        {
            _logger.LogWarning("HSS found but PluginInterface type is missing");
            return false;
        }

        var registerMethod = pluginInterfaceType.GetMethod("RegisterSection", BindingFlags.Public | BindingFlags.Static);
        if (registerMethod == null)
        {
            _logger.LogWarning("HSS PluginInterface.RegisterSection method not found");
            return false;
        }

        var payload = new JObject
        {
            { "id", SectionId.ToString() },
            { "displayText", "Live on Twitch" },
            { "limit", 1 },
            { "route", string.Empty },
            { "additionalData", string.Empty },
            { "resultsAssembly", typeof(TwitchResultsHandler).Assembly.FullName },
            { "resultsClass", typeof(TwitchResultsHandler).FullName },
            { "resultsMethod", nameof(TwitchResultsHandler.GetResults) }
        };

        var parseMethod = GetTargetJObjectParseMethod(assembly);
        if (parseMethod == null)
        {
            _logger.LogWarning("Could not find JObject.Parse in HSS assembly's AssemblyLoadContext");
            return false;
        }

        try
        {
            var json = payload.ToString();
            var targetJObject = parseMethod.Invoke(null, new object[] { json });
            registerMethod.Invoke(null, new[] { targetJObject });
            _logger.LogDebug("HSS RegisterSection invoked successfully");
            return true;
        }
        catch (TargetInvocationException ex) when (ex.InnerException is InvalidOperationException)
        {
            _logger.LogDebug("HSS not ready yet: {Message}", ex.InnerException.Message);
            return false;
        }
    }

    private MethodInfo? GetTargetJObjectParseMethod(Assembly targetAssembly)
    {
        var targetContext = AssemblyLoadContext.GetLoadContext(targetAssembly);
        var assemblies = targetContext != null
            ? targetContext.Assemblies
            : AssemblyLoadContext.All.SelectMany(x => x.Assemblies);

        foreach (var asm in assemblies)
        {
            if (asm.GetName().Name != "Newtonsoft.Json")
            {
                continue;
            }

            var jObjectType = asm.GetType("Newtonsoft.Json.Linq.JObject");
            var parseMethod = jObjectType?.GetMethod("Parse", BindingFlags.Public | BindingFlags.Static, new[] { typeof(string) });
            if (parseMethod != null)
            {
                return parseMethod;
            }
        }

        foreach (var asm in AssemblyLoadContext.All.SelectMany(x => x.Assemblies))
        {
            if (asm.GetName().Name != "Newtonsoft.Json")
            {
                continue;
            }

            var jObjectType = asm.GetType("Newtonsoft.Json.Linq.JObject");
            var parseMethod = jObjectType?.GetMethod("Parse", BindingFlags.Public | BindingFlags.Static, new[] { typeof(string) });
            if (parseMethod != null)
            {
                return parseMethod;
            }
        }

        return null;
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
