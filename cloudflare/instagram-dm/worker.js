/**
 * Instagram comment -> DM, on Cloudflare (user, 2026-11-18: "go with the Cloudflare way", not a GitHub Actions schedule).
 *
 * Every 2 minutes (Cron Trigger): our reels of the last 7 days whose comment count changed are read; a comment that names
 * a keyword (job, jobs, link, daily jobs, telegram, channel) or is only emoji gets one "private reply": a card with
 * "Telegram kholo" / "Follow karo" buttons (plain text if Instagram refuses the card). Instagram allows one private reply
 * per comment, within 7 days of it. The same logic, for manual check / dry-run runs, is tools/instagram-dm in this repo.
 *
 * What happens after the DM (user, 2026-11-18: how many followed / joined Telegram, in the morning report):
 *  - the buttons point at this Worker (/go/tg/<comment>, /go/follow/<comment>), which counts the first click per DM and
 *    redirects (to the "Instagram DM" invite link of the channel, or to the profile)
 *  - joins through that invite link: the bot reads the channel's chat_member updates (getUpdates), which name the link
 *  - follows: 3 hours after the DM, is_user_follow_business for the recipient (Instagram may only answer it for people
 *    who wrote to us; then it is counted as "unknown")
 *  GET /stats: per-day counts (no names). The report (tools/report) reads it.
 *
 * KV (DM_STATE): start, n:<media> (comment count seen), c:<comment> (answered, 8 d), d:<comment> (the DM: recipient id,
 * clicks, follow check; 60 d), pending (DMs waiting for their follow check), stats (per-day counts), tg:invite, tg:offset,
 * last-run. Secrets: IG_ACCESS_TOKEN, TELEGRAM_BOT_TOKEN (optional: no join count without it). Vars: IG_USER_ID,
 * PUBLIC_URL, TELEGRAM_CHANNEL.
 */

const GRAPH = "https://graph.facebook.com/v26.0";
const MAX_AGE_DAYS = 7;
const FOLLOW_CHECK_AFTER_MS = 3 * 3600_000;
const KEYWORDS = ["job", "jobs", "link", "daily jobs", "telegram", "channel"];
const EMOJI_ONLY = true;
const PROFILE_URL = "https://www.instagram.com/isha.07007/";
const CHANNEL_URL = "https://telegram.me/fresherjobs_isha";
const CARD_TEXT = {
  title: "Roz ki fresher jobs 📌",
  subtitle: "Seedha company ki site se, apply link ke saath. Reels ke liye follow kar lo 🙂",
};
const TEXT =
  "Hi! 👋 Roz ki fresher jobs (seedha company ki site se, apply link ke saath) yahan milti hain:\n" +
  `${CHANNEL_URL}\n\nAur interview tips ke reels ke liye @isha.07007 ko follow kar lo 🙂`;
const EMPTY_DAY = { dms: 0, tgClicks: 0, followClicks: 0, tgJoins: 0, followsYes: 0, followsNo: 0, followsUnknown: 0 };

export function wantsLink(text) {
  const t = (text || "").trim().toLowerCase();
  if (!t) return false;
  const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (KEYWORDS.some((k) => new RegExp(`(?<![a-z])${escape(k)}(?![a-z])`).test(t))) return true;
  // only emoji (and spaces / punctuation): "🔥", "🙏🙏", "❤️"
  const rest = t.replace(/[\p{Extended_Pictographic}\u{FE0F}\u{200D}\u{1F3FB}-\u{1F3FF}]/gu, "");
  return EMOJI_ONLY && /\p{Extended_Pictographic}/u.test(t) && !/[\p{L}\p{N}]/u.test(rest);
}

// ---------- small helpers ----------

const istDay = (ms = Date.now()) => new Date(ms + 5.5 * 3600_000).toISOString().slice(0, 10);

async function bump(env, field, n = 1, when = Date.now()) {
  const stats = JSON.parse((await env.DM_STATE.get("stats")) || "{}");
  const day = istDay(when);
  stats[day] = { ...EMPTY_DAY, ...(stats[day] || {}) };
  stats[day][field] += n;
  for (const d of Object.keys(stats).sort().slice(0, -60)) delete stats[d]; // keep 60 days
  await env.DM_STATE.put("stats", JSON.stringify(stats));
}

async function graph(env, path, params = {}, body = null) {
  const url = new URL(`${GRAPH}/${path}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  url.searchParams.set("access_token", env.IG_ACCESS_TOKEN);
  const res = await fetch(url, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) {
    const e = data.error || {};
    throw new Error(`${body ? "POST" : "GET"} ${path}: HTTP ${res.status}: ${e.message || "error"} (code ${e.code}, subcode ${e.error_subcode})`);
  }
  return data;
}

async function telegram(env, method, params) {
  const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  const data = await res.json().catch(() => ({}));
  if (!data.ok) throw new Error(`telegram ${method}: ${data.description || res.status}`); // never the URL: it holds the token
  return data.result;
}

// ---------- DM ----------

function card(env, commentId) {
  const base = (env.PUBLIC_URL || "").replace(/\/$/, "");
  const via = (kind, direct) => (base ? `${base}/go/${kind}/${encodeURIComponent(commentId)}` : direct);
  return {
    ...CARD_TEXT,
    buttons: [
      { type: "web_url", title: "Telegram kholo", url: via("tg", CHANNEL_URL) },
      { type: "web_url", title: "Follow karo", url: via("follow", PROFILE_URL) },
    ],
  };
}

async function reply(env, commentId) {
  const recipient = { comment_id: commentId };
  try {
    const r = await graph(env, `${env.IG_USER_ID}/messages`, {}, {
      recipient,
      message: { attachment: { type: "template", payload: { template_type: "generic", elements: [card(env, commentId)] } } },
    });
    return { kind: "card", igsid: r.recipient_id || "" };
  } catch (e) {
    if (/\(code (10|200|190),/.test(e.message)) throw e; // permission / token: the text would fail the same way
    const r = await graph(env, `${env.IG_USER_ID}/messages`, {}, { recipient, message: { text: TEXT } });
    return { kind: "text", igsid: r.recipient_id || "" };
  }
}

export async function run(env, dryRun = false) {
  // Comments from before the Worker's first run were handled by the GitHub workflow (or need no answer): only newer ones
  let start = await env.DM_STATE.get("start");
  if (!start) {
    start = new Date().toISOString();
    if (!dryRun) await env.DM_STATE.put("start", start);
  }
  const reelsSince = Date.now() - MAX_AGE_DAYS * 86400_000; // Instagram: private replies only within 7 days
  const since = Math.max(reelsSince, Date.parse(start));
  const own = (await graph(env, env.IG_USER_ID, { fields: "username" })).username || "";
  const media = (await graph(env, `${env.IG_USER_ID}/media`, { fields: "id,timestamp,permalink,comments_count", limit: 30 })).data || [];
  const out = { at: new Date().toISOString(), reels: 0, read: 0, matched: 0, sent: 0, failed: 0, log: [] };
  for (const m of media) {
    if (Date.parse(m.timestamp) < reelsSince) break;
    out.reels++;
    const count = m.comments_count || 0;
    const seen = await env.DM_STATE.get(`n:${m.id}`);
    if (!count || String(count) === seen) continue; // nothing new on this reel
    out.read++;
    const res = await graph(env, `${m.id}/comments`, { fields: "id,text,timestamp,username,replies{id,text,timestamp,username}", limit: 50 });
    let ok = true;
    for (const c of res.data || []) {
      for (const x of [c, ...((c.replies && c.replies.data) || [])]) {
        if ((x.username || "").toLowerCase() === own.toLowerCase() || Date.parse(x.timestamp) < since) continue;
        if (!wantsLink(x.text) || (await env.DM_STATE.get(`c:${x.id}`))) continue;
        out.matched++;
        if (dryRun) {
          out.log.push(`would DM @${x.username}: ${JSON.stringify((x.text || "").slice(0, 60))}`);
          continue;
        }
        let done = true;
        try {
          const { kind, igsid } = await reply(env, x.id);
          out.sent++;
          out.log.push(`DM (${kind}) to @${x.username}: ${JSON.stringify((x.text || "").slice(0, 60))}`);
          await env.DM_STATE.put(`d:${x.id}`, JSON.stringify({ at: Date.now(), kind, igsid }), { expirationTtl: 60 * 86400 });
          await bump(env, "dms");
          if (igsid) {
            const pending = JSON.parse((await env.DM_STATE.get("pending")) || "[]");
            pending.push(x.id);
            await env.DM_STATE.put("pending", JSON.stringify(pending.slice(-500)));
          }
        } catch (e) {
          out.failed++;
          out.log.push(`not sent to @${x.username}: ${e.message.slice(0, 200)}`);
          // a token / permission / rate problem: leave it for the next run. Anything else (already replied, comment too
          // old or gone) won't get better by trying again.
          if (/\(code (10|200|190|4|17|32|613),/.test(e.message)) done = ok = false;
        }
        if (done) await env.DM_STATE.put(`c:${x.id}`, new Date().toISOString(), { expirationTtl: 8 * 86400 });
      }
    }
    if (ok && !dryRun) await env.DM_STATE.put(`n:${m.id}`, String(count), { expirationTtl: 9 * 86400 });
  }
  if (!dryRun) {
    out.follows = await checkFollows(env).catch((e) => `follow check failed: ${e.message.slice(0, 160)}`);
    out.joins = await countJoins(env).catch((e) => `join count failed: ${e.message.slice(0, 160)}`);
  }
  if (!dryRun && (out.matched || out.read)) {
    // only the counts: GET / is public, so no commenter names or texts there (those go to the Worker's own logs)
    const { log, ...counts } = out;
    await env.DM_STATE.put("last-run", JSON.stringify(counts));
  }
  return out;
}

// ---------- after the DM ----------

/** 3 h after a DM: does the recipient follow us now? Counted on the day of the DM. */
export async function checkFollows(env, limit = 10) {
  const pending = JSON.parse((await env.DM_STATE.get("pending")) || "[]");
  if (!pending.length) return "none pending";
  const keep = [];
  let checked = 0;
  for (const id of pending) {
    const d = JSON.parse((await env.DM_STATE.get(`d:${id}`)) || "null");
    if (!d || !d.igsid) continue;
    if (checked >= limit || Date.now() - d.at < FOLLOW_CHECK_AFTER_MS) {
      keep.push(id);
      continue;
    }
    checked++;
    try {
      const p = await graph(env, d.igsid, { fields: "is_user_follow_business" });
      d.follows = p.is_user_follow_business === true;
    } catch (e) {
      d.follows = "unknown"; // Instagram answers this only for people who wrote to us
      d.followError = e.message.slice(0, 120);
    }
    await env.DM_STATE.put(`d:${id}`, JSON.stringify(d), { expirationTtl: 60 * 86400 });
    await bump(env, d.follows === true ? "followsYes" : d.follows === false ? "followsNo" : "followsUnknown", 1, d.at);
  }
  await env.DM_STATE.put("pending", JSON.stringify(keep));
  return `${checked} checked`;
}

// telegram.me, not t.me: some Indian ISPs block t.me at DNS (2026-11-09). The invite link itself stays t.me in KV,
// because chat_member updates name it that way.
const toTelegramMe = (url) => url.replace(/^https:\/\/t\.me\//, "https://telegram.me/");

/** The channel's "Instagram DM" invite link (made once; the bot needs the "Invite users via link" admin right). */
async function inviteLink(env) {
  if (!env.TELEGRAM_BOT_TOKEN) return CHANNEL_URL;
  const known = await env.DM_STATE.get("tg:invite");
  if (known) return known === "none" ? CHANNEL_URL : toTelegramMe(known);
  try {
    const link = await telegram(env, "createChatInviteLink", { chat_id: env.TELEGRAM_CHANNEL, name: "Instagram DM" });
    await env.DM_STATE.put("tg:invite", link.invite_link);
    return toTelegramMe(link.invite_link);
  } catch (e) {
    console.error(`no invite link (${e.message}); the plain channel link is used, so joins from DMs can't be told apart`);
    await env.DM_STATE.put("tg:invite", "none", { expirationTtl: 86400 }); // try again tomorrow
    return CHANNEL_URL;
  }
}

/** Joins through the DM invite link since the last look (chat_member updates; Telegram keeps them 24 h). */
export async function countJoins(env) {
  if (!env.TELEGRAM_BOT_TOKEN) return "no bot token";
  await inviteLink(env);
  const invite = await env.DM_STATE.get("tg:invite");
  const offset = Number((await env.DM_STATE.get("tg:offset")) || 0);
  const updates = await telegram(env, "getUpdates", { offset, limit: 100, timeout: 0, allowed_updates: ["chat_member"] });
  let joins = 0;
  for (const u of updates) {
    const cm = u.chat_member;
    const was = cm && cm.old_chat_member && cm.old_chat_member.status;
    const now = cm && cm.new_chat_member && cm.new_chat_member.status;
    if (cm && cm.invite_link && cm.invite_link.invite_link === invite && ["left", "kicked"].includes(was) && now === "member") joins++;
  }
  if (updates.length) await env.DM_STATE.put("tg:offset", String(updates[updates.length - 1].update_id + 1));
  if (joins) await bump(env, "tgJoins", joins);
  return `${joins} joined via the DM link`;
}

// ---------- HTTP ----------

async function click(env, kind, commentId) {
  const d = JSON.parse((await env.DM_STATE.get(`d:${commentId}`)) || "null");
  if (d && !d[kind]) {
    d[kind] = Date.now(); // the first click per DM counts
    await env.DM_STATE.put(`d:${commentId}`, JSON.stringify(d), { expirationTtl: 60 * 86400 });
    await bump(env, kind === "tg" ? "tgClicks" : "followClicks", 1, d.at);
  }
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(
      run(env).then(
        (r) => console.log(JSON.stringify(r)),
        (e) => console.error(`instagram-dm run failed: ${e.message}`)
      )
    );
  },
  async fetch(request, env, ctx) {
    const path = new URL(request.url).pathname;
    const go = path.match(/^\/go\/(tg|follow)\/([\w-]{1,64})$/);
    if (go) {
      const [, kind, id] = go;
      ctx.waitUntil(click(env, kind, id).catch((e) => console.error(`click: ${e.message}`)));
      return Response.redirect(kind === "tg" ? await inviteLink(env) : PROFILE_URL, 302);
    }
    const json = (body) => new Response(body, { headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" } });
    // counts only, per IST day of the DM: no names, no comment texts, nothing secret
    if (path === "/stats") return json((await env.DM_STATE.get("stats")) || "{}");
    if (path === "/") return json((await env.DM_STATE.get("last-run")) || '{"note":"no run yet"}');
    return new Response("not found", { status: 404 });
  },
};
