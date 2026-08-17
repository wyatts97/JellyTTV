using System;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.JellyTTV;

/// <summary>
/// Injects the JellyTTV client script and stylesheet into the Jellyfin index.html
/// response at runtime. This avoids modifying files on disk and works on read-only
/// installations (Docker, package-managed, etc.).
/// </summary>
public class JellyTTVStartupFilter : IStartupFilter
{
    private readonly ILogger<JellyTTVStartupFilter> _logger;
    private readonly JellyTTVScriptManager _scriptManager;

    /// <summary>
    /// Initializes a new instance of the <see cref="JellyTTVStartupFilter"/> class.
    /// </summary>
    public JellyTTVStartupFilter(ILogger<JellyTTVStartupFilter> logger, JellyTTVScriptManager scriptManager)
    {
        _logger = logger;
        _scriptManager = scriptManager;
    }

    /// <inheritdoc />
    public Action<IApplicationBuilder> Configure(Action<IApplicationBuilder> next)
    {
        return app =>
        {
            app.Use(async (context, nextMiddleware) =>
            {
                if (_scriptManager.IsExternal)
                {
                    await nextMiddleware().ConfigureAwait(false);
                    return;
                }
                var path = context.Request.Path.Value ?? string.Empty;
                var isIndex = path.EndsWith("/index.html", StringComparison.OrdinalIgnoreCase)
                              || path.Equals("/web", StringComparison.OrdinalIgnoreCase);

                if (!isIndex || !IsHtmlResponse(context))
                {
                    await nextMiddleware().ConfigureAwait(false);
                    return;
                }

                var originalBody = context.Response.Body;
                await using var capture = new MemoryStream();
                context.Response.Body = capture;

                await nextMiddleware().ConfigureAwait(false);

                if (context.Response.StatusCode != 200)
                {
                    await CopyToOriginalAsync(capture, originalBody).ConfigureAwait(false);
                    context.Response.Body = originalBody;
                    return;
                }

                capture.Seek(0, SeekOrigin.Begin);
                var html = await new StreamReader(capture, Encoding.UTF8).ReadToEndAsync().ConfigureAwait(false);

                const string marker = "<!-- JellyTTV Plugin BEGIN -->";
                if (!html.Contains(marker, StringComparison.OrdinalIgnoreCase))
                {
                    var headEnd = html.IndexOf("</head>", StringComparison.OrdinalIgnoreCase);
                    if (headEnd >= 0)
                    {
                        html = html.Insert(headEnd, JellyTTVScriptManager.GetInjectionHtml());
                        _logger.LogInformation("Injected JellyTTV client into {Path}", path);
                    }
                    else
                    {
                        _logger.LogWarning("No </head> tag found in {Path}", path);
                    }
                }

                var bytes = Encoding.UTF8.GetBytes(html);
                context.Response.ContentLength = bytes.Length;
                context.Response.Body = originalBody;
                await originalBody.WriteAsync(bytes, 0, bytes.Length).ConfigureAwait(false);
            });

            next(app);
        };
    }

    private static bool IsHtmlResponse(HttpContext context)
    {
        if (context.Response.HasStarted)
        {
            return false;
        }

        var contentType = context.Response.ContentType;
        return contentType != null && contentType.Contains("text/html", StringComparison.OrdinalIgnoreCase);
    }

    private static async Task CopyToOriginalAsync(Stream source, Stream destination)
    {
        if (source.Length > 0)
        {
            source.Seek(0, SeekOrigin.Begin);
            await source.CopyToAsync(destination).ConfigureAwait(false);
        }
    }
}
