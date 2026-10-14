// The X feed for the news tracker. Lives in a Google Sheet (Extensions > Apps Script).
//
// X refuses GitHub's servers but answers Google's (tested 14 Oct 2026), so
// this does the one job GitHub cannot: every five minutes it reads the
// bell-notification feed of @DeepakatPTI and adds any new
// post to the "X feed" tab. The tracker on GitHub asks for the recent ones
// on every run (doGet below) and judges them like any other source
// (tracker/xfeed.py). Only accounts the burner follows WITH THE BELL ON
// appear here.
//
// The login codes are kept in Project Settings > Script properties, never
// in the sheet: AUTH_TOKEN and CT0, copied from x.com's cookies while
// logged in as @DeepakatPTI. When X logs it out, refresh those two.
//
// First time: run setup() once. It makes the tab, starts the five-minute
// timer and does a first read.

const SHEET = 'X feed';
const KEEP_ROWS = 3000;          // about a week at the current pace
const FEED_URL =
  'https://x.com/i/api/2/notifications/device_follow.json?count=40&tweet_mode=extended';
// The public token X's own website uses; not a secret.
const WEB_BEARER =
  'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA';
const HEADERS = ['id', 'posted (UTC)', 'handle', 'name', 'text', 'link', 'repost', 'reply'];

function setup() {
  const book = SpreadsheetApp.getActive();
  const sheet = book.getSheetByName(SHEET) || book.insertSheet(SHEET);
  sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]).setFontWeight('bold');
  sheet.setFrozenRows(1);
  sheet.setColumnWidth(5, 600);

  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'checkX')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('checkX').timeBased().everyMinutes(5).create();

  checkX();
  const props = PropertiesService.getScriptProperties();
  Logger.log(props.getProperty('error')
    ? 'PROBLEM: ' + props.getProperty('error')
    : 'Working. ' + (sheet.getLastRow() - 1) + ' posts in the "' + SHEET + '" tab.');
}

function checkX() {
  const props = PropertiesService.getScriptProperties();
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;      // the previous check is still running
  const now = new Date().toISOString();
  try {
    const found = readFeed_(props);
    const sheet = SpreadsheetApp.getActive().getSheetByName(SHEET);
    const count = sheet.getLastRow() - 1;
    const known = new Set(count > 0
      ? sheet.getRange(2, 1, count, 1).getValues().map(r => String(r[0]))
      : []);
    const fresh = found.filter(p => !known.has(p.id))
      .sort((a, b) => (BigInt(b.id) > BigInt(a.id) ? 1 : -1));

    if (fresh.length) {
      // Newest at the top. The leading apostrophe keeps ids and times as
      // text: a sheet would round a 19-digit id and turn a time into a date.
      sheet.insertRowsBefore(2, fresh.length);
      sheet.getRange(2, 1, fresh.length, HEADERS.length).setValues(fresh.map(p => [
        "'" + p.id, "'" + p.at, p.handle, p.name, p.text, p.url, p.repost, p.reply,
      ]));
      const extra = sheet.getLastRow() - 1 - KEEP_ROWS;
      if (extra > 0) sheet.deleteRows(KEEP_ROWS + 2, extra);
    }
    props.setProperties({ last_ok: now, last_try: now, error: '' });
  } catch (err) {
    props.setProperties({ last_try: now, error: String(err).slice(0, 300) });
  } finally {
    lock.releaseLock();
  }
}

// Newest first, every post in the feed (the tracker picks which accounts it wants).
function readFeed_(props) {
  const ct0 = props.getProperty('CT0');
  const auth = props.getProperty('AUTH_TOKEN');
  if (!ct0 || !auth) throw new Error('AUTH_TOKEN and CT0 are missing from Script properties');

  const res = UrlFetchApp.fetch(FEED_URL, {
    muteHttpExceptions: true,
    headers: {
      authorization: 'Bearer ' + WEB_BEARER,
      'x-csrf-token': ct0,
      cookie: 'auth_token=' + auth + '; ct0=' + ct0,
      'x-twitter-active-user': 'yes',
      'x-twitter-auth-type': 'OAuth2Session',
    },
  });
  const code = res.getResponseCode();
  if (code !== 200) {
    throw new Error('X replied ' + code + (code === 401 || code === 403
      ? ' -- the @DeepakatPTI login has probably expired; refresh AUTH_TOKEN and CT0'
      : '') + ': ' + res.getContentText().slice(0, 150));
  }
  const data = JSON.parse(res.getContentText()).globalObjects || {};
  const users = data.users || {};
  return Object.values(data.tweets || {}).map(t => {
    const user = users[t.user_id_str] || {};
    const handle = user.screen_name || t.user_id_str;
    let text = t.full_text || t.text || '';
    ((t.entities && t.entities.urls) || []).forEach(u => {
      text = text.split(u.url).join(u.expanded_url || u.url);
    });
    text = text.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
    return {
      id: t.id_str,
      at: new Date(t.created_at).toISOString(),
      handle: handle,
      name: user.name || '',
      text: text,
      url: 'https://x.com/' + handle + '/status/' + t.id_str,
      repost: Boolean(t.retweeted_status_id_str) || /^RT @/.test(text),
      // A reply to someone else; a thread continuing one's own post is not.
      reply: Boolean(t.in_reply_to_status_id_str) && t.in_reply_to_user_id_str !== t.user_id_str,
    };
  });
}

// What the tracker reads: ?hours=12 gives every post from the last 12 hours,
// plus when X was last read successfully, so a dead login is noticed.
function doGet(e) {
  const hours = Number((e && e.parameter && e.parameter.hours) || 12);
  const since = Date.now() - hours * 3600 * 1000;
  const props = PropertiesService.getScriptProperties();
  const sheet = SpreadsheetApp.getActive().getSheetByName(SHEET);
  const count = sheet.getLastRow() - 1;
  const rows = count > 0 ? sheet.getRange(2, 1, count, HEADERS.length).getValues() : [];
  const posts = [];
  for (const r of rows) {
    if (new Date(r[1]).getTime() < since) continue;
    posts.push({ id: String(r[0]), at: String(r[1]), handle: r[2], name: r[3], text: r[4],
                 url: r[5], repost: r[6] === true, reply: r[7] === true });
  }
  return ContentService.createTextOutput(JSON.stringify({
    last_ok: props.getProperty('last_ok') || '',
    last_try: props.getProperty('last_try') || '',
    error: props.getProperty('error') || '',
    posts: posts,
  })).setMimeType(ContentService.MimeType.JSON);
}
