import wixData from 'wix-data';
import { ok, badRequest, serverError } from 'wix-http-functions';
import { fetch } from 'wix-fetch';
import { getSecret } from 'wix-secrets-backend';
import { posts, categories } from 'wix-blog-backend';

function cors() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Yankeesfarm-Secret, x-yankeesfarm-secret"
  };
}

function noCache() {
  return {
    ...cors(),
    "Cache-Control": "private, no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "Vary": "*",
    "ETag": 'W/"' + Date.now() + '"',
  };
}

export async function post_incrementView(request) {
  try {
    const body = await request.body.json();
    const slug = body.slug;
    if (!slug) return badRequest({ headers: cors(), body: { error: "Missing slug" } });
    const results = await wixData.query("ProspectViews").eq("slug", slug).find();
    let newCount;
    if (results.items.length > 0) {
      const item = results.items[0];
      item.views = (item.views || 0) + 1;
      item.viewsThisYear = (item.viewsThisYear || 0) + 1;
      await wixData.update("ProspectViews", item);
      newCount = item.views;
    } else {
      newCount = 1;
      await wixData.insert("ProspectViews", { slug, views: 1, viewsThisYear: 1 });
    }

    // Daily log for 30-day tracking (one row per slug per day).
    // Wrapped separately so a log problem can never break the main view count.
    try {
      const dateKey = new Date().toISOString().slice(0, 10);
      const logRes = await wixData.query("ProspectViewLog").eq("slug", slug).eq("dateKey", dateKey).find({ suppressAuth: true });
      if (logRes.items.length > 0) {
        const logItem = logRes.items[0];
        logItem.count = (logItem.count || 0) + 1;
        await wixData.update("ProspectViewLog", logItem, { suppressAuth: true });
      } else {
        await wixData.insert("ProspectViewLog", { slug, dateKey, count: 1 }, { suppressAuth: true });
      }
    } catch (logErr) {
      // ignore: log collection missing or unavailable
    }

    return ok({ headers: cors(), body: { views: newCount } });
  } catch (err) {
    return serverError({ headers: cors(), body: { error: err.message } });
  }
}
export async function options_incrementView(request) { return ok({ headers: cors() }); }

export async function get_getPlayerVideos(request) {
  try {
    const { playlistId, firstName, lastName } = request.query;
    if (!playlistId || !firstName || !lastName) {
      return badRequest({ headers: noCache(), body: { error: "Missing playlistId, firstName, or lastName" } });
    }
    const apiKey = await getSecret("youtube-api-key");
    const url = `https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&maxResults=50&playlistId=${encodeURIComponent(playlistId)}&key=${apiKey}`;
    const res = await fetch(url, { method: "get" });
    const data = await res.json();
    const first = firstName.toLowerCase();
    const last = lastName.toLowerCase();
    const videoIds = (data.items || [])
      .filter(item => {
        const title = (item.snippet?.title || "").toLowerCase();
        return title.includes(first) && title.includes(last);
      })
      .slice(-4)
      .map(item => item.snippet.resourceId.videoId);
    return ok({ headers: noCache(), body: { videoIds } });
  } catch (err) {
    return serverError({ headers: noCache(), body: { error: err.message } });
  }
}
export async function options_getPlayerVideos(request) { return ok({ headers: cors() }); }

export async function get_getPlayerArticles(request) {
  try {
    const { firstName, lastName } = request.query;
    if (!firstName || !lastName) {
      return badRequest({ headers: noCache(), body: { error: "Missing firstName or lastName" } });
    }
    const categoryResults = await categories.queryCategories().eq("label", "Prospect Spotlight").find();
    if (!categoryResults.items.length) {
      return ok({ headers: noCache(), body: { articles: [] } });
    }
    const categoryId = categoryResults.items[0]._id;
    const postResults = await posts.queryPosts().hasSome("categoryIds", [categoryId]).find();
    const first = firstName.toLowerCase();
    const last = lastName.toLowerCase();
    const matches = postResults.items.filter(p => {
      const searchableText = [p.title, p.excerpt, p.plainContent, p.contentText].filter(Boolean).join(" ").toLowerCase();
      return searchableText.includes(first) && searchableText.includes(last);
    });
    const articles = matches.map(p => ({
      title: p.title, excerpt: p.excerpt,
      url: p.url ? p.url.base + p.url.path : null,
      coverImage: p.coverMedia && p.coverMedia.image ? p.coverMedia.image.url : null
    }));
    return ok({ headers: noCache(), body: { articles } });
  } catch (err) {
    return serverError({ headers: noCache(), body: { error: err.message } });
  }
}
export async function options_getPlayerArticles(request) { return ok({ headers: cors() }); }

export async function get_getPlayerStats(request) {
  try {
    const { slug } = request.query;
    if (!slug) return badRequest({ headers: noCache(), body: { error: "Missing slug" } });
    const results = await wixData.query("ProspectStats").eq("slug", slug).find();
    if (!results.items.length) {
      return ok({ headers: noCache(), body: { found: false } });
    }
    const item = results.items[0];
    return ok({ headers: noCache(), body: {
      found: true,
      seasons: item.seasonsJSON ? JSON.parse(item.seasonsJSON) : null,
      splits: item.splitsJSON ? JSON.parse(item.splitsJSON) : null,
      lastUpdated: item.lastUpdated
    }});
  } catch (err) {
    return serverError({ headers: noCache(), body: { error: err.message } });
  }
}
export async function options_getPlayerStats(request) { return ok({ headers: cors() }); }

export async function post_updatePlayerStats(request) {
  try {
    const body = await request.body.json();
    const expectedKey = await getSecret("daily-stats-push-key");
    if (!body.key || body.key !== expectedKey) {
      return badRequest({ headers: cors(), body: { error: "Invalid key" } });
    }
    if (!body.slug) return badRequest({ headers: cors(), body: { error: "Missing slug" } });
    const existing = await wixData.query("ProspectStats").eq("slug", body.slug).find();
    const record = {
      slug: body.slug,
      seasonsJSON: JSON.stringify(body.seasons || null),
      splitsJSON: JSON.stringify(body.splits || null),
      lastUpdated: new Date()
    };
    if (existing.items.length) {
      record._id = existing.items[0]._id;
      await wixData.update("ProspectStats", record, { suppressAuth: true });
    } else {
      await wixData.insert("ProspectStats", record, { suppressAuth: true });
    }
    return ok({ headers: cors(), body: { success: true } });
  } catch (err) {
    return serverError({ headers: cors(), body: { error: err.message } });
  }
}
export async function options_updatePlayerStats(request) { return ok({ headers: cors() }); }

export async function post_top_performers(request) {
  try {
    const headers = request.headers || {};
    const providedSecret = headers["x-yankeesfarm-secret"] || headers["X-Yankeesfarm-Secret"];
    const expectedSecret = await getSecret("YANKEESFARM_PUSH_SECRET");
    if (!providedSecret || providedSecret !== expectedSecret) {
      return badRequest({ headers: cors(), body: { error: "Invalid or missing secret" } });
    }
    const payload = await request.body.json();
    await wixData.insert("TopPerformers", { ...payload, pushedAt: new Date() }, { suppressAuth: true });
    return ok({ headers: cors(), body: { success: true } });
  } catch (err) {
    return serverError({ headers: cors(), body: { error: err.message } });
  }
}
// Top Performers is in-season only: Mar 1 - Sep 30 (America/New_York).
function isTopPerformersSeason() {
  const month = parseInt(new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "numeric" }).format(new Date()), 10);
  return month >= 3 && month <= 9;
}

export async function get_top_performers(request) {
  try {
    if (!isTopPerformersSeason()) {
      return ok({ headers: noCache(), body: { date: null, generatedAt: null, hitters: [], pitchers: [], playerOfTheDay: null, pitcherOfTheDay: null, offseason: true } });
    }
    const results = await wixData.query("TopPerformers").descending("pushedAt").limit(1).find({ suppressAuth: true });
    if (!results.items.length) {
      return ok({ headers: noCache(), body: { date: null, generatedAt: null, hitters: [], pitchers: [], playerOfTheDay: null, pitcherOfTheDay: null } });
    }
    const item = results.items[0];
    return ok({
      headers: noCache(),
      body: {
        date: item.date || null,
        generatedAt: item.generatedAt || null,
        hitters: item.hitters || [],
        pitchers: item.pitchers || [],
        playerOfTheDay: item.playerOfTheDay || null,
        pitcherOfTheDay: item.pitcherOfTheDay || null,
      },
    });
  } catch (err) {
    return serverError({ headers: noCache(), body: { error: err.message } });
  }
}
export async function options_top_performers(request) { return ok({ headers: cors() }); }

const LEADERBOARD_CATEGORY_META = {
  hitting_avg: { label: "Batting Average", group: "hitting", isRate: true },
  hitting_obp: { label: "On-Base Percentage", group: "hitting", isRate: true },
  hitting_slg: { label: "Slugging Percentage", group: "hitting", isRate: true },
  hitting_ops: { label: "OPS", group: "hitting", isRate: true },
  hitting_hits: { label: "Hits", group: "hitting", isRate: false },
  hitting_doubles: { label: "Doubles", group: "hitting", isRate: false },
  hitting_triples: { label: "Triples", group: "hitting", isRate: false },
  hitting_homeRuns: { label: "Home Runs", group: "hitting", isRate: false },
  hitting_rbi: { label: "RBI", group: "hitting", isRate: false },
  hitting_stolenBases: { label: "Stolen Bases", group: "hitting", isRate: false },
  hitting_bbPct: { label: "BB%", group: "hitting", isRate: true },
  hitting_kPct: { label: "K%", group: "hitting", isRate: true },
  hitting_kMinusBbPct: { label: "K-BB%", group: "hitting", isRate: true },
  hitting_xbhPct: { label: "XBH%", group: "hitting", isRate: true },
  hitting_iso: { label: "ISO", group: "hitting", isRate: true },
  hitting_babip: { label: "BABIP", group: "hitting", isRate: true },
  hitting_woba: { label: "wOBA", group: "hitting", isRate: true },
  pitching_era: { label: "ERA", group: "pitching", isRate: true },
  pitching_whip: { label: "WHIP", group: "pitching", isRate: true },
  pitching_ip_outs: { label: "Innings Pitched", group: "pitching", isRate: false },
  pitching_strikeOuts: { label: "Strikeouts", group: "pitching", isRate: false },
  pitching_avg: { label: "AVG Against", group: "pitching", isRate: true },
  pitching_so9: { label: "SO/9", group: "pitching", isRate: true },
  pitching_kPct: { label: "K%", group: "pitching", isRate: true },
  pitching_bbPct: { label: "BB%", group: "pitching", isRate: true },
  pitching_kMinusBbPct: { label: "K-BB%", group: "pitching", isRate: true },
  pitching_fip: { label: "FIP", group: "pitching", isRate: true },
};

export async function post_leaderboards(request) {
  try {
    const headers = request.headers || {};
    const providedSecret = headers["x-yankeesfarm-secret"] || headers["X-Yankeesfarm-Secret"];
    const expectedSecret = await getSecret("YANKEESFARM_PUSH_SECRET");
    if (!providedSecret || providedSecret !== expectedSecret) {
      return badRequest({ headers: cors(), body: { error: "Invalid or missing secret" } });
    }
    const payload = await request.body.json();
    const period = payload.period;
    const generatedAt = payload.generatedAt;
    const minPA = payload.meta && payload.meta.minPA;
    const minIP = payload.meta && payload.meta.minIP;
    if (!period || !payload.categories) {
      return badRequest({ headers: cors(), body: { error: "Missing period or categories" } });
    }
    const existing = await wixData.query("LeaderboardEntries").eq("period", period).limit(1000).find({ suppressAuth: true });
    if (existing.items.length) {
      await wixData.bulkRemove("LeaderboardEntries", existing.items.map(item => item._id), { suppressAuth: true });
    }
    const rows = [];
    for (const [categoryKey, cat] of Object.entries(payload.categories)) {
      for (const entry of cat.entries || []) {
        rows.push({ category: categoryKey, playerName: entry.name, team: entry.team, value: entry.value, rank: entry.rank, period, generatedAt, minPa: minPA, minIp: minIP });
      }
    }
    if (rows.length) {
      await wixData.bulkInsert("LeaderboardEntries", rows, { suppressAuth: true });
    }
    return ok({ headers: cors(), body: { success: true, rowsWritten: rows.length } });
  } catch (err) {
    return serverError({ headers: cors(), body: { error: err.message } });
  }
}
export async function get_leaderboards(request) {
  try {
    const period = request.query.period || "weekly";
    const results = await wixData.query("LeaderboardEntries").eq("period", period).limit(1000).find({ suppressAuth: true });
    const items = results.items;
    if (!items.length) {
      return ok({ headers: noCache(), body: { categories: {}, generatedAt: null, minPA: null, minIP: null } });
    }
    const latestGeneratedAt = items.reduce((max, item) => (item.generatedAt > max ? item.generatedAt : max), items[0].generatedAt);
    const latestItems = items.filter(item => item.generatedAt === latestGeneratedAt);
    const categories2 = {};
    for (const item of latestItems) {
      const meta = LEADERBOARD_CATEGORY_META[item.category] || { label: item.category, group: "hitting", isRate: false };
      if (!categories2[item.category]) {
        categories2[item.category] = { label: meta.label, group: meta.group, isRate: meta.isRate, entries: [] };
      }
      categories2[item.category].entries.push({ rank: item.rank, isTied: typeof item.rank === "string" && item.rank.startsWith("T"), name: item.playerName, team: item.team, value: item.value });
    }
    for (const cat of Object.values(categories2)) {
      cat.entries.sort((a, b) => parseInt(a.rank, 10) - parseInt(b.rank, 10) || 0);
    }
    return ok({ headers: noCache(), body: { generatedAt: latestGeneratedAt, minPA: latestItems[0].minPa != null ? latestItems[0].minPa : null, minIP: latestItems[0].minIp != null ? latestItems[0].minIp : null, categories: categories2 } });
  } catch (err) {
    return serverError({ headers: noCache(), body: { error: err.message } });
  }
}
export async function options_leaderboards(request) { return ok({ headers: cors() }); }

export async function post_analytics(request) {
  try {
    const headers = request.headers || {};
    const providedSecret = headers["x-yankeesfarm-secret"] || headers["X-Yankeesfarm-Secret"];
    const expectedSecret = await getSecret("YANKEESFARM_PUSH_SECRET");
    if (!providedSecret || providedSecret !== expectedSecret) {
      return badRequest({ headers: cors(), body: { error: "Invalid or missing secret" } });
    }
    const payload = await request.body.json();
    if (!Array.isArray(payload.hitters)) {
      return badRequest({ headers: cors(), body: { error: "Missing hitters array" } });
    }
    await wixData.save("FarmAnalyticsSnapshot", { _id: "latest", season: payload.season, generated_at: payload.generated_at, hitters: payload.hitters, pitchers: payload.pitchers || [], teams: payload.teams || [] }, { suppressAuth: true });
    return ok({ headers: cors(), body: { success: true } });
  } catch (err) {
    return serverError({ headers: cors(), body: { error: err.message } });
  }
}
export async function get_analytics(request) {
  try {
    const result = await wixData.get("FarmAnalyticsSnapshot", "latest", { suppressAuth: true });
    if (!result) {
      return ok({ headers: noCache(), body: { hitters: [], pitchers: [], teams: [], generated_at: null } });
    }
    return ok({ headers: noCache(), body: { season: result.season, generated_at: result.generated_at, hitters: result.hitters || [], pitchers: result.pitchers || [], teams: result.teams || [] } });
  } catch (err) {
    return serverError({ headers: noCache(), body: { error: err.message } });
  }
}
export async function options_analytics(request) { return ok({ headers: cors() }); }

export async function get_topProfiles(request) {
  try {
    const allResults = await wixData.query("ProspectViews")
      .descending("views")
      .limit(10)
      .find({ suppressAuth: true });

    const allItems = await wixData.query("ProspectViews")
      .gt("views", 0)
      .limit(1000)
      .find({ suppressAuth: true });

    const allTime = allResults.items.map(i => ({
      slug: i.slug,
      views: i.views || 0,
      viewsThisYear: i.viewsThisYear || 0
    }));

    const thisYear = [...allItems.items]
      .filter(i => (i.viewsThisYear || 0) > 0)
      .sort((a, b) => (b.viewsThisYear || 0) - (a.viewsThisYear || 0))
      .slice(0, 10)
      .map(i => ({
        slug: i.slug,
        views: i.views || 0,
        viewsThisYear: i.viewsThisYear || 0
      }));

    // Last 30 days from the daily log (dateKey is a YYYY-MM-DD string)
    let last30Days = [];
    try {
      const thirtyDaysAgoKey = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
      const logRes = await wixData.query("ProspectViewLog")
        .ge("dateKey", thirtyDaysAgoKey)
        .limit(1000)
        .find({ suppressAuth: true });
      const totals = {};
      for (const log of logRes.items) {
        totals[log.slug] = (totals[log.slug] || 0) + (log.count || 0);
      }
      last30Days = Object.entries(totals)
        .sort((x, y) => y[1] - x[1])
        .slice(0, 10)
        .map(([slug, viewsLast30]) => ({ slug, views: viewsLast30, viewsLast30 }));
    } catch (logErr) {
      // log collection missing: leave last30Days empty
    }

    return ok({ headers: noCache(), body: { allTime, thisYear, last30Days } });
  } catch (err) {
    return serverError({ headers: noCache(), body: { error: err.message } });
  }
}
export async function options_topProfiles(request) { return ok({ headers: cors() }); }