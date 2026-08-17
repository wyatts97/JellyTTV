/**
 * JellyTTV Plugin — Client Script
 *
 * Injects a "Twitch" sidebar nav link and a "Live on Twitch" home screen section
 * into Jellyfin's web UI. Fetches live channel data from the plugin's proxy API.
 */
(function () {
    'use strict';

    var JellyTTV = {
        config: null,
        liveChannels: [],
        knownLiveLogins: {},
        pollTimer: null,
        navInjected: false,
        homeInjected: false,

        init: function () {
            var self = this;
            this.fetchConfig().then(function () {
                if (!self.config) return;

                if (self.config.EnableSidebarLink) {
                    self.injectSidebarLink();
                }
                if (self.config.EnableHomeSection) {
                    self.injectHomeSection();
                }
                if (self.config.EnableNotifications) {
                    self.startNotificationPolling();
                }

                // Start polling for live data
                self.refreshData().then(function () {
                    self.renderAll();
                });

                // Set up periodic refresh
                var interval = Math.max(10, self.config.RefreshIntervalSeconds || 60) * 1000;
                self.pollTimer = setInterval(function () {
                    self.refreshData().then(function () {
                        self.renderAll();
                    });
                }, interval);

                // Re-inject on navigation (Jellyfin SPA)
                self.observeNavigation();
            });
        },

        // ── Config ──────────────────────────────────────────────

        fetchConfig: function () {
            var self = this;
            return fetch('/JellyTTV/Live', { headers: { 'Accept': 'application/json' } })
                .then(function (res) { return res.ok ? res.json() : null; })
                .then(function (data) {
                    // Config is derived from what the plugin returns alongside live data.
                    // For now, use sensible defaults — the plugin C# side controls caching.
                    self.config = {
                        EnableSidebarLink: true,
                        EnableHomeSection: true,
                        EnableNotifications: true,
                        RefreshIntervalSeconds: 60
                    };
                    return data;
                })
                .catch(function () {
                    self.config = null;
                });
        },

        // ── Data Fetching ───────────────────────────────────────

        refreshData: function () {
            var self = this;
            return fetch('/JellyTTV/Live', { headers: { 'Accept': 'application/json' } })
                .then(function (res) {
                    if (!res.ok) return null;
                    return res.json();
                })
                .then(function (data) {
                    if (!data || !data.channels) return;
                    self.liveChannels = data.channels.filter(function (c) { return c.is_live; });

                    // Check for newly live streamers (for notifications)
                    if (self.config.EnableNotifications) {
                        self.checkNewLive();
                    }
                })
                .catch(function () {
                    // silent fail — will retry on next poll
                });
        },

        // ── Notifications ───────────────────────────────────────

        checkNewLive: function () {
            var self = this;
            var newlyLive = [];
            self.liveChannels.forEach(function (ch) {
                if (!self.knownLiveLogins[ch.login] && Object.keys(self.knownLiveLogins).length > 0) {
                    newlyLive.push(ch);
                }
                self.knownLiveLogins[ch.login] = true;
            });

            newlyLive.forEach(function (ch) {
                self.showNotification(ch);
            });
        },

        showNotification: function (ch) {
            try {
                if (typeof require !== 'undefined') {
                    var toast = require(['toast']);
                    toast(ch.display_name + ' is live: ' + (ch.title || ch.game_name || ''), 5000);
                } else if (window.ApiClient && window.ApiClient.displayNotification) {
                    window.ApiClient.displayNotification(ch.display_name + ' is live!', { type: 'info' });
                }
            } catch (e) {
                // Fallback: console
                console.log('[JellyTTV] ' + ch.display_name + ' went live');
            }
            this.updateNavBadge();
        },

        // ── Sidebar Navigation ──────────────────────────────────

        injectSidebarLink: function () {
            var self = this;
            if (this.navInjected) return;

            function tryInject() {
                var navList = document.querySelector('.mainDrawer-scrollContainer') ||
                              document.querySelector('.navDrawer .scrollContainer') ||
                              document.querySelector('[data-role="drawer"] .list');

                if (!navList) {
                    setTimeout(tryInject, 500);
                    return;
                }

                if (navList.querySelector('.jellyttv-nav-link')) {
                    self.navInjected = true;
                    return;
                }

                var link = document.createElement('a');
                link.className = 'navLink jellyttv-nav-link';
                link.href = '#';
                link.setAttribute('data-role', 'button');
                link.innerHTML =
                    '<span class="navLinkOption jellyttv-nav-icon">' +
                    '<svg class="jellyttv-icon" width="24" height="24" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">' +
                    '<defs><linearGradient id="jttv-grad" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">' +
                    '<stop stop-color="#a970ff"/><stop offset="1" stop-color="#00a4dc"/>' +
                    '</linearGradient></defs>' +
                    '<rect width="64" height="64" rx="14" fill="url(#jttv-grad)"/>' +
                    '<path d="M16 14h32v22l-9 9h-8l-6 6h-5v-6h-4V14Z" fill="#fff" opacity=".92"/>' +
                    '<rect x="30" y="21" width="4" height="11" rx="2" fill="#4b1f9c"/>' +
                    '<rect x="39" y="21" width="4" height="11" rx="2" fill="#4b1f9c"/>' +
                    '</svg>' +
                    '</span>' +
                    '<span class="navLinkText">Twitch</span>' +
                    '<span class="jellyttv-nav-badge" style="display:none;">0</span>';

                link.addEventListener('click', function (e) {
                    e.preventDefault();
                    self.showTwitchPage();
                });

                // Insert after "Live TV" if it exists, otherwise at a reasonable position
                var liveTvLink = null;
                var links = navList.querySelectorAll('a, .navLink');
                for (var i = 0; i < links.length; i++) {
                    var text = (links[i].textContent || '').trim().toLowerCase();
                    if (text === 'live tv' || text.indexOf('live tv') >= 0) {
                        liveTvLink = links[i];
                        break;
                    }
                }

                if (liveTvLink && liveTvLink.nextSibling) {
                    navList.insertBefore(link, liveTvLink.nextSibling);
                } else if (liveTvLink) {
                    navList.appendChild(link);
                } else {
                    // Insert before the admin/settings section
                    var adminLink = navList.querySelector('[data-id="dashboard"], [href*="dashboard"]');
                    if (adminLink) {
                        navList.insertBefore(link, adminLink);
                    } else {
                        navList.appendChild(link);
                    }
                }

                self.navInjected = true;
                self.updateNavBadge();
            }

            tryInject();
        },

        updateNavBadge: function () {
            var badge = document.querySelector('.jellyttv-nav-badge');
            if (!badge) return;
            var count = this.liveChannels.length;
            if (count > 0) {
                badge.textContent = count;
                badge.style.display = 'inline-block';
            } else {
                badge.style.display = 'none';
            }
        },

        // ── Home Screen Section ─────────────────────────────────

        injectHomeSection: function () {
            var self = this;
            if (this.homeInjected) return;

            function tryInject() {
                var homeContainer = document.querySelector('.homeSectionsContainer') ||
                                    document.querySelector('[data-role="content"] .content-primary') ||
                                    document.querySelector('.homePage');

                if (!homeContainer) {
                    // Only try on home page
                    if (!self.isOnHomePage()) {
                        return;
                    }
                    setTimeout(tryInject, 500);
                    return;
                }

                if (document.querySelector('.jellyttv-home-section')) {
                    self.homeInjected = true;
                    return;
                }

                var section = document.createElement('div');
                section.className = 'jellyttv-home-section verticalSection';
                section.innerHTML =
                    '<div class="sectionTitleContainer flex align-items-center">' +
                    '<h2 class="sectionTitle sectionTitle-cards">' +
                    '<span class="material-icons jellyttv-section-icon" style="vertical-align: middle; margin-right: 0.3em; font-size: 1.2em;">live_tv</span>' +
                    'Live on Twitch' +
                    '</h2>' +
                    '</div>' +
                    '<div class="jellyttv-cards-row itemsContainer cardsContainer horizontal-wrap"></div>';

                // Insert at the top of the home page
                homeContainer.insertBefore(section, homeContainer.firstChild);
                self.homeInjected = true;
                self.renderHomeCards();
            }

            tryInject();
        },

        renderHomeCards: function () {
            var row = document.querySelector('.jellyttv-cards-row');
            if (!row) return;

            row.innerHTML = '';

            if (this.liveChannels.length === 0) {
                row.innerHTML = '<div class="jellyttv-empty">No streamers are currently live.</div>';
                return;
            }

            this.liveChannels.forEach(function (ch) {
                row.appendChild(JellyTTV.createCard(ch, 'home'));
            });
        },

        // ── Twitch Page (full view) ─────────────────────────────

        showTwitchPage: function () {
            var self = this;
            var contentArea = document.querySelector('.content-primary') ||
                              document.querySelector('[data-role="content"]') ||
                              document.querySelector('.mainAnimatedPages');

            if (!contentArea) return;

            // Clear existing content
            var existing = contentArea.querySelector('.jellyttv-page');
            if (existing) {
                existing.remove();
            }

            var page = document.createElement('div');
            page.className = 'jellyttv-page page';
            page.innerHTML =
                '<div class="jellyttv-page-header">' +
                '<h1>' +
                '<span class="material-icons jellyttv-section-icon" style="vertical-align: middle; margin-right: 0.3em;">live_tv</span>' +
                'Twitch Live Channels' +
                '</h1>' +
                '<span class="jellyttv-live-count">' + this.liveChannels.length + ' live</span>' +
                '</div>' +
                '<div class="jellyttv-page-grid itemsContainer cardsContainer"></div>';

            // Hide other content
            var children = contentArea.children;
            for (var i = 0; i < children.length; i++) {
                if (children[i] !== page && children[i].style) {
                    children[i].style.display = 'none';
                }
            }

            contentArea.appendChild(page);

            var grid = page.querySelector('.jellyttv-page-grid');
            if (this.liveChannels.length === 0) {
                grid.innerHTML = '<div class="jellyttv-empty">No streamers are currently live. Check back later!</div>';
            } else {
                this.liveChannels.forEach(function (ch) {
                    grid.appendChild(self.createCard(ch, 'page'));
                });
            }
        },

        // ── Card Rendering ──────────────────────────────────────

        createCard: function (ch, context) {
            var card = document.createElement('div');
            card.className = 'card jellyttv-card ' + (context === 'home' ? 'jellyttv-home-card' : 'jellyttv-page-card');

            var thumbUrl = ch.thumbnail_url || '';
            var avatarUrl = ch.avatar_url || '';
            var viewerCount = ch.viewer_count > 0 ? this.formatViewers(ch.viewer_count) : '';
            var gameName = ch.game_name || '';
            var title = ch.title || ch.display_name || ch.login;

            var imgHtml = thumbUrl
                ? '<img class="jellyttv-card-image" src="' + thumbUrl + '" alt="' + this.escapeHtml(ch.display_name) + '" loading="lazy" />'
                : '<div class="jellyttv-card-image jellyttv-card-placeholder"><span class="material-icons">live_tv</span></div>';

            var avatarHtml = avatarUrl
                ? '<img class="jellyttv-card-avatar" src="' + avatarUrl + '" alt="" loading="lazy" />'
                : '<div class="jellyttv-card-avatar jellyttv-card-avatar-placeholder"><span class="material-icons">person</span></div>';

            card.innerHTML =
                '<div class="jellyttv-card-thumb">' +
                imgHtml +
                '<span class="jellyttv-live-badge">LIVE</span>' +
                (viewerCount ? '<span class="jellyttv-viewer-count"><span class="material-icons" style="font-size: 0.85em;">visibility</span> ' + viewerCount + '</span>' : '') +
                '</div>' +
                '<div class="jellyttv-card-info">' +
                avatarHtml +
                '<div class="jellyttv-card-text">' +
                '<div class="jellyttv-card-title" title="' + this.escapeHtml(title) + '">' + this.escapeHtml(title) + '</div>' +
                (gameName ? '<div class="jellyttv-card-game">' + this.escapeHtml(gameName) + '</div>' : '') +
                '<div class="jellyttv-card-streamer">' + this.escapeHtml(ch.display_name || ch.login) + '</div>' +
                '</div>' +
                '</div>' +
                '<button class="jellyttv-card-watch" data-login="' + this.escapeHtml(ch.login) + '">' +
                '<span class="material-icons" style="font-size: 1.1em;">play_circle</span> Watch' +
                '</button>';

            // Watch button
            var watchBtn = card.querySelector('.jellyttv-card-watch');
            if (watchBtn) {
                var self = this;
                watchBtn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    self.watchStream(ch);
                });
            }

            // Image error handling
            var img = card.querySelector('.jellyttv-card-image');
            if (img && img.tagName === 'IMG') {
                img.addEventListener('error', function () {
                    img.style.display = 'none';
                    var placeholder = document.createElement('div');
                    placeholder.className = 'jellyttv-card-image jellyttv-card-placeholder';
                    placeholder.innerHTML = '<span class="material-icons">live_tv</span>';
                    img.parentNode.insertBefore(placeholder, img);
                });
            }

            return card;
        },

        // ── Watch Stream ────────────────────────────────────────

        watchStream: function (ch) {
            // Navigate to the Live TV channel for this streamer
            // Jellyfin uses the channel name from the M3U playlist
            var channelName = ch.login || ch.display_name;
            try {
                // Try to find and play the Live TV channel
                if (window.ApiClient) {
                    window.ApiClient.getItems({
                        IncludeItemTypes: 'LiveTvChannel',
                        SearchTerm: channelName,
                        Limit: 1
                    }).then(function (result) {
                        if (result.Items && result.Items.length > 0) {
                            var itemId = result.Items[0].Id;
                            window.location.href = '/item.html?id=' + itemId + '&play=1';
                        } else {
                            console.warn('[JellyTTV] No Live TV channel found for ' + channelName);
                        }
                    }).catch(function () {
                        console.warn('[JellyTTV] Could not search for Live TV channel');
                    });
                }
            } catch (e) {
                console.error('[JellyTTV] Error navigating to stream', e);
            }
        },

        // ── Rendering ───────────────────────────────────────────

        renderAll: function () {
            this.renderHomeCards();
            this.updateNavBadge();

            // Update page view if open
            var page = document.querySelector('.jellyttv-page');
            if (page) {
                var countEl = page.querySelector('.jellyttv-live-count');
                if (countEl) {
                    countEl.textContent = this.liveChannels.length + ' live';
                }
                var grid = page.querySelector('.jellyttv-page-grid');
                if (grid) {
                    grid.innerHTML = '';
                    var self = this;
                    if (this.liveChannels.length === 0) {
                        grid.innerHTML = '<div class="jellyttv-empty">No streamers are currently live. Check back later!</div>';
                    } else {
                        this.liveChannels.forEach(function (ch) {
                            grid.appendChild(self.createCard(ch, 'page'));
                        });
                    }
                }
            }
        },

        // ── Navigation Observer ─────────────────────────────────

        observeNavigation: function () {
            var self = this;

            // Jellyfin uses hash-based or pushState navigation
            // We use a MutationObserver on the main content area
            var observer = new MutationObserver(function (mutations) {
                // Re-inject sidebar if it was re-rendered
                if (self.config && self.config.EnableSidebarLink && !document.querySelector('.jellyttv-nav-link')) {
                    self.navInjected = false;
                    self.injectSidebarLink();
                }
                // Re-inject home section if we're on the home page
                if (self.config && self.config.EnableHomeSection && self.isOnHomePage() && !document.querySelector('.jellyttv-home-section')) {
                    self.homeInjected = false;
                    self.injectHomeSection();
                }
            });

            var target = document.querySelector('.mainAnimatedPages') || document.body;
            if (target) {
                observer.observe(target, { childList: true, subtree: true });
            }
        },

        isOnHomePage: function () {
            return window.location.hash.indexOf('home') >= 0 ||
                   window.location.pathname === '/' ||
                   window.location.pathname === '/index.html' ||
                   window.location.hash === '' ||
                   window.location.hash === '#';
        },

        // ── Utilities ───────────────────────────────────────────

        formatViewers: function (count) {
            if (count >= 1000000) return (count / 1000000).toFixed(1) + 'M';
            if (count >= 1000) return (count / 1000).toFixed(1) + 'K';
            return String(count);
        },

        escapeHtml: function (str) {
            if (!str) return '';
            return str
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }
    };

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { JellyTTV.init(); });
    } else {
        JellyTTV.init();
    }

    // Expose for debugging
    window.JellyTTV = JellyTTV;
})();
