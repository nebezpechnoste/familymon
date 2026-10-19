// familymon lesson block — the "already open tab" half of lesson-time web
// blocking. Chrome's URLBlocklist (written by familymon) stops navigation
// to blocked sites, but a tab loaded before the lesson keeps its connection.
// This worker reads the same block set from managed storage (the `3rdparty`
// policy familymon writes next to URLBlocklist) and, while a lesson is in
// progress, moves every matching tab to the blocked page.
import { blockedHost, normaliseConfig, tabHost } from "./rules.js";

const KEYS = ["active", "blocked", "allowed", "subject", "until"];

async function loadConfig() {
  try {
    return normaliseConfig(await chrome.storage.managed.get(KEYS));
  } catch {
    return normaliseConfig(null);
  }
}

function blockedPage(host, cfg) {
  const q = new URLSearchParams({ host, subject: cfg.subject, until: cfg.until });
  return chrome.runtime.getURL("blocked.html") + "?" + q.toString();
}

async function closeTab(tab, cfg) {
  const host = tabHost(tab.url);
  if (host === null || tab.id === undefined) return false;
  if (blockedHost(host, cfg.blocked, cfg.allowed) === null) return false;
  try {
    await chrome.tabs.update(tab.id, { url: blockedPage(host, cfg) });
    return true;
  } catch {
    return false;
  }
}

// Sweep every open tab against `cfg`; returns the number redirected.
async function sweep(cfg) {
  if (!cfg.active || cfg.blocked.length === 0) return 0;
  const tabs = await chrome.tabs.query({});
  let n = 0;
  for (const tab of tabs) {
    if (await closeTab(tab, cfg)) n += 1;
  }
  return n;
}

async function sweepFromPolicy() {
  return sweep(await loadConfig());
}

chrome.runtime.onInstalled.addListener(() => void sweepFromPolicy());
chrome.runtime.onStartup.addListener(() => void sweepFromPolicy());
// Policy changes land here within seconds of familymon rewriting its file.
chrome.storage.onChanged.addListener((_changes, area) => {
  if (area === "managed") void sweepFromPolicy();
});
// Backstop for anything that slipped past URLBlocklist (e.g. a tab that was
// mid-load when the policy flipped).
chrome.tabs.onUpdated.addListener((_tabId, info, tab) => {
  if (info.url) void loadConfig().then((cfg) => cfg.active && closeTab(tab, cfg));
});
chrome.alarms.create("familymon-sweep", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "familymon-sweep") void sweepFromPolicy();
});

// Test hook: lets a harness drive a sweep with an explicit config. Only code
// running inside this extension's own context can reach it.
self.__familymonSweep = (raw) => sweep(normaliseConfig(raw));
self.__familymonConfig = loadConfig;
