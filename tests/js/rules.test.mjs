import { test } from "node:test";
import assert from "node:assert/strict";
import { blockedHost, normaliseConfig, tabHost } from "../../familymon/chrome_extension/rules.js";

const BLOCKED = ["web.whatsapp.com", "roblox.com", "discord.com"];

test("exact host and subdomains are blocked", () => {
  assert.equal(blockedHost("roblox.com", BLOCKED), "roblox.com");
  assert.equal(blockedHost("www.roblox.com", BLOCKED), "roblox.com");
  assert.equal(blockedHost("WEB.WhatsApp.com", BLOCKED), "web.whatsapp.com");
  assert.equal(blockedHost("discord.com.", BLOCKED), "discord.com");
});

test("lookalikes and unrelated hosts pass", () => {
  assert.equal(blockedHost("notroblox.com", BLOCKED), null);
  assert.equal(blockedHost("roblox.com.evil.net", BLOCKED), null);
  assert.equal(blockedHost("whatsapp.com", BLOCKED), null); // only web. listed here
  assert.equal(blockedHost("kingsinterhigh.co.uk", BLOCKED), null);
  assert.equal(blockedHost("", BLOCKED), null);
});

test("allowed entries win over blocked ones", () => {
  assert.equal(blockedHost("meet.google.com", ["google.com"], ["meet.google.com"]), null);
  assert.equal(blockedHost("docs.google.com", ["google.com"], ["meet.google.com"]), "google.com");
});

test("tabHost only sees web pages", () => {
  assert.equal(tabHost("https://web.whatsapp.com/"), "web.whatsapp.com");
  assert.equal(tabHost("http://example.com:8080/x?y"), "example.com");
  assert.equal(tabHost("chrome://newtab/"), null);
  assert.equal(tabHost("chrome-extension://abcdefghijklmnopabcdefghijklmnop/blocked.html"), null);
  assert.equal(tabHost("file:///tmp/x.html"), null);
  assert.equal(tabHost("not a url"), null);
  assert.equal(tabHost(undefined), null);
});

test("normaliseConfig tolerates garbage", () => {
  assert.deepEqual(normaliseConfig(undefined), { active: false, blocked: [], allowed: [], subject: "", until: "" });
  assert.deepEqual(normaliseConfig({ active: "yes", blocked: "x", subject: 1 }),
    { active: false, blocked: [], allowed: [], subject: "", until: "" });
  assert.deepEqual(
    normaliseConfig({ active: true, blocked: ["a.com", 3], allowed: ["b.com"], subject: "Maths", until: "10:00" }),
    { active: true, blocked: ["a.com"], allowed: ["b.com"], subject: "Maths", until: "10:00" });
});
