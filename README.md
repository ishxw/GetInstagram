# Instagram reader service

This directory contains a small Python standard-library service for the local
Instagram reader page. It makes unauthenticated requests to public Instagram
HTML and returns the post order, permalink, media preview, caption, and
timestamp when Instagram includes that data in the page.

## Run

```powershell
cd E:\Project\Web\MyWeb\instagram-reader
python server.py --host 127.0.0.1 --port 8787
```

The service listens on `http://127.0.0.1:8787` by default. The frontend can call:

```text
GET /api/profile?url=https%3A%2F%2Fwww.instagram.com%2Fusername%2F
```

默认返回每条帖子的详细信息，并按发布时间从新到旧排序。传入
`pinned=true` 可保留 Instagram 页面顺序（包括置顶帖）：

```text
GET /api/profile?url=https%3A%2F%2Fwww.instagram.com%2Fusername%2F&pinned=true
```

只需要主页当前可见链接时，使用 `details=false` 或 `links_only=true`，此模式
不会逐个请求帖子页面，速度更快：

```text
GET /api/profile?url=https%3A%2F%2Fwww.instagram.com%2Fusername%2F&details=false
```

也可以使用 POST JSON 请求同一个接口：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8787/api/profile `
  -ContentType 'application/json' `
  -Body '{"url":"https://www.instagram.com/username/","details":false}'
```

The same endpoint also accepts a single post or Reel URL, for example
`https://www.instagram.com/minecraft/reel/Dd4CEw5pR21/`, and returns one item
with its public caption, publish time, and media preview. Carousel posts expose
all available media items, and Reels expose a playable video source when the
anonymous page provides one.

Open `http://127.0.0.1:8787/` in a browser to use the bundled page. The same
server hosts the HTML, CSS, JavaScript, and API, so no separate static server
or browser CORS setup is needed.

The response is JSON with `ok`, `profile`, `posts`, `links`, `count`, `mode`,
`sort`, and `warnings`. In `mode=links`, `links` and `posts[].url` are the
available post URLs and no detail page is requested. In `mode=details`,
`profile` also includes `full_name`, `biography`, `avatar`, `is_verified`,
`followers`, `following`, and `external_url` when Instagram exposes them.
Each post includes `order`, `url`, `caption`, `timestamp`, `preview_url`,
`image_url`, and `video_url`. `detail_fetch.ok` shows
whether the individual post page could be read. A login wall, rate limit, or
missing anonymous data is returned as a warning or per-post error instead of
being treated as a successful read.

The API sends permissive CORS headers for local static frontends. Only
`instagram.com`, `www.instagram.com`, and `m.instagram.com` profile or post
URLs are accepted by the endpoint.
