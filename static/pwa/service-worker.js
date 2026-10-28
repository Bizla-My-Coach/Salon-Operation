'use strict';
const CACHE_PREFIX = 'hairship-offline-';
const CACHE_NAME = CACHE_PREFIX + 'v1';
const OFFLINE_URL = '/offline/';

self.addEventListener('install', function (event) {
  event.waitUntil(caches.open(CACHE_NAME).then(function (cache) {
    // Only this public reconnect page is stored. Staff pages and API responses
    // always go to the server, including after switching accounts or signing out.
    return cache.add(new Request(OFFLINE_URL, { cache: 'reload', credentials: 'omit' }));
  }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (key) {
      return key.indexOf(CACHE_PREFIX) === 0 && key !== CACHE_NAME;
    }).map(function (key) { return caches.delete(key); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (event) {
  // Never intercept form submissions, task actions, or background API requests.
  if (event.request.method !== 'GET' || event.request.mode !== 'navigate') return;
  event.respondWith(fetch(event.request).catch(function () {
    return caches.match(OFFLINE_URL).then(function (page) {
      return page || new Response('You are offline. Reconnect and open Hairship again.', {
        status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      });
    });
  }));
});
