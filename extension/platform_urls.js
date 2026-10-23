"use strict";
/* Keep canonical URLs identical to native_host/platform_urls.py (shared fixtures). */
function classifySourceUrl(raw) {
  const value = String(raw || "").trim();
  const unknown = (label = "无法识别") => ({ platform: "", kind: "unknown", label, normalizedUrl: value });
  const source = (platform, kind, normalizedUrl) => ({
    platform, kind, normalizedUrl,
    label: {
      youtube: { video: "视频", playlist: "列表", channel: "频道" },
      bilibili: { video: "视频", favorites: "收藏夹", collection: "合集", shortlink: "短链接" },
      kugou: { song: "歌曲", playlist: "歌单" },
    }[platform][kind],
  });
  const number = (x) => /^[1-9][0-9]*$/.test(x || "");
  const youtubeId = (x) => /^[A-Za-z0-9_-]+$/.test(x || "");
  try {
    if (!value || /[\x00-\x20\x7f\\]/.test(value)) return unknown("URL 无效");
    const url = new URL(value);
    const authority = value.match(/^[A-Za-z]+:\/\/([^/?#]*)/)?.[1] || "";
    if (!["https:", "http:"].includes(url.protocol) || !authority || /[@%]/.test(authority) || url.username || url.password || url.port) {
      return unknown("URL 无效");
    }
    const originalPath = value.match(/^[A-Za-z]+:\/\/[^/?#]*(\/[^?#]*)?/)?.[1] || "";
    if (/(?:^|\/)(?:\.|%2e){1,2}(?=\/|$)/i.test(originalPath)) return unknown("URL 无效");
    const host = url.hostname.toLowerCase();
    const path = url.pathname.replace(/\/+$/, "");
    const parts = url.pathname.split("/").filter(Boolean);
    const q = url.searchParams;

    if (["youtu.be", "www.youtu.be"].includes(host)) {
      return youtubeId(parts[0]) ? source("youtube", "video", `https://www.youtube.com/watch?v=${parts[0]}`) : unknown();
    }
    if (["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"].includes(host)) {
      if (["/playlist", "/watch"].includes(path) && youtubeId(q.get("list"))) {
        return source("youtube", "playlist", `https://www.youtube.com/playlist?list=${q.get("list")}`);
      }
      if (path === "/watch" && youtubeId(q.get("v"))) return source("youtube", "video", `https://www.youtube.com/watch?v=${q.get("v")}`);
      if (["shorts", "live", "embed"].includes(parts[0]) && youtubeId(parts[1])) {
        return source("youtube", "video", `https://www.youtube.com/watch?v=${parts[1]}`);
      }
      const root = parts[0]?.startsWith("@") && parts[0].length > 1 ? parts.slice(0, 1)
        : ["channel", "c", "user"].includes(parts[0]) && parts[1] ? parts.slice(0, 2) : null;
      if (root) {
        const canonical = root.map((part) => encodeURIComponent(part)
          .replace(/%25/g, "%").replace(/%40/g, "@")
          .replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)).join("/");
        return source("youtube", "channel", `https://www.youtube.com/${canonical}`);
      }
      return unknown("非视频/列表/频道");
    }
    if (host === "b23.tv") {
      return parts.length === 1 && /^[A-Za-z0-9]+$/.test(parts[0])
        ? source("bilibili", "shortlink", `https://b23.tv/${parts[0]}`) : unknown();
    }
    if (host === "space.bilibili.com") {
      if (number(parts[0])) {
        if (parts.length === 2 && parts[1] === "favlist" && number(q.get("fid"))) {
          return source("bilibili", "favorites", `https://www.bilibili.com/medialist/detail/ml${q.get("fid")}`);
        }
        if (parts.length === 3 && parts[1] === "lists" && number(parts[2])) {
          const type = q.get("type") || "season";
          if (["season", "series"].includes(type)) return source("bilibili", "collection", `https://space.bilibili.com/${parts[0]}/lists/${parts[2]}?type=${type}`);
        }
        if (parts.length === 3 && parts[1] === "channel" && ["collectiondetail", "seriesdetail"].includes(parts[2]) && number(q.get("sid"))) {
          const type = parts[2] === "collectiondetail" ? "season" : "series";
          return source("bilibili", "collection", `https://space.bilibili.com/${parts[0]}/lists/${q.get("sid")}?type=${type}`);
        }
      }
      return unknown("请选择具体收藏夹/合集");
    }
    if (["bilibili.com", "www.bilibili.com", "m.bilibili.com"].includes(host)) {
      const favorites = path.match(/^\/(?:medialist\/(?:detail|play)|list)\/ml([1-9][0-9]*)$/);
      if (favorites) return source("bilibili", "favorites", `https://www.bilibili.com/medialist/detail/ml${favorites[1]}`);
      if (parts.length === 2 && parts[0] === "list" && number(parts[1]) && number(q.get("sid"))) {
        if (q.has("otype") && !/^[0-9]+$/.test(q.get("otype"))) return unknown("集合类型无效");
        return source("bilibili", "collection", `https://www.bilibili.com/list/${parts[1]}?sid=${q.get("sid")}${q.has("otype") ? `&otype=${q.get("otype")}` : ""}`);
      }
      if (parts.length === 3 && parts[0] === "medialist" && parts[1] === "play" && number(parts[2]) && number(q.get("business_id"))) {
        const business = q.get("business");
        if (["space_series", "space_collection"].includes(business)) {
          const type = business === "space_series" ? "series" : "season";
          return source("bilibili", "collection", `https://space.bilibili.com/${parts[2]}/lists/${q.get("business_id")}?type=${type}`);
        }
      }
      if (parts.length === 2 && parts[0] === "video" && /^(?:BV[A-Za-z0-9]{10}|av[1-9][0-9]*)$/i.test(parts[1])) {
        if (q.has("p") && !number(q.get("p"))) return unknown("分 P 编号无效");
        const id = (parts[1].slice(0, 2).toLowerCase() === "bv" ? "BV" : "av") + parts[1].slice(2);
        return source("bilibili", "video", `https://www.bilibili.com/video/${id}${q.get("p") ? `?p=${q.get("p")}` : ""}`);
      }
      return unknown("非视频/收藏夹/合集");
    }
    if (["kugou.com", "www.kugou.com", "m.kugou.com"].includes(host)) {
      const mixsong = path.match(/^\/mixsong\/([A-Za-z0-9]+)\.html$/);
      if (mixsong) return source("kugou", "song", `https://www.kugou.com/mixsong/${mixsong[1]}.html`);
      const special = path.match(/^\/yy\/special\/single\/([^/]+)\.html$/);
      const mobile = host === "m.kugou.com" ? path.match(/^\/plist\/list\/([1-9][0-9]*)$/) : null;
      const id = special?.[1] || mobile?.[1] || "";
      if (/^(?:[1-9][0-9]*|collection_(?:[0-9]+_){3}[0-9]+)$/.test(id)) {
        return source("kugou", "playlist", `https://www.kugou.com/yy/special/single/${id}.html`);
      }
      if (path === "/song" || (path === "/yy/index.php" && q.get("r") === "play/getdata")) {
        const fragment = new URLSearchParams(url.hash.slice(1));
        const songHash = fragment.has("hash") ? fragment.get("hash") : q.get("hash");
        const albumId = fragment.has("album_id") ? fragment.get("album_id") : q.get("album_id");
        if (/^[A-Fa-f0-9]{32}$/.test(songHash || "") && (!albumId || /^[0-9]+$/.test(albumId))) {
          return source("kugou", "song", `https://www.kugou.com/song/#hash=${songHash.toUpperCase()}${albumId ? `&album_id=${albumId}` : ""}`);
        }
      }
      return unknown("请选择歌单或 hash 歌曲页");
    }
    return unknown("非支持站点");
  } catch (_) {
    return unknown("URL 无效");
  }
}

if (typeof module !== "undefined" && module.exports) module.exports = { classifySourceUrl };
