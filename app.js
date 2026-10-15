const form = document.querySelector('#lookupForm');
const input = document.querySelector('#profileUrl');
const lookupButton = document.querySelector('#lookupButton');
const clearButton = document.querySelector('#clearButton');
const refreshButton = document.querySelector('#refreshButton');
const statePanel = document.querySelector('#statePanel');
const stateIcon = document.querySelector('#stateIcon');
const stateTitle = document.querySelector('#stateTitle');
const stateMessage = document.querySelector('#stateMessage');
const results = document.querySelector('#results');
const postList = document.querySelector('#postList');
const emptyResults = document.querySelector('#emptyResults');
const formHint = document.querySelector('#formHint');
const sortMode = document.querySelector('#sortMode');

let activeUrl = '';

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  }[character]));
}

// Instagram's anonymous HTML can contain indentation and several blank lines
// around a caption. Keep paragraph breaks, but remove formatting whitespace
// that would otherwise make a short caption look like a very tall block.
function normalizeDisplayText(value) {
  return String(value ?? '')
    .replace(/\r\n?/g, '\n')
    .replace(/\u00a0/g, ' ')
    .replace(/[\u200b\ufeff]/g, '')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n[ \t]+/g, '\n')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function renderTextBlock(text, className, emptyText, threshold = 520) {
  const normalized = normalizeDisplayText(text);
  if (!normalized) {
    return `<p class="${className} muted">${escapeHtml(emptyText)}</p>`;
  }
  const isLong = normalized.length > threshold || normalized.split('\n').length > 9;
  const body = `<p class="${className}">${escapeHtml(normalized)}</p>`;
  if (!isLong) return body;
  return `<div class="text-preview is-collapsed">${body}<button class="text-toggle" type="button" aria-expanded="false">展开全文</button></div>`;
}

function normalizePayload(payload) {
  const source = payload?.data && typeof payload.data === 'object' ? payload.data : payload;
  const rawPosts = Array.isArray(source?.posts) ? source.posts : Array.isArray(source?.items) ? source.items : [];
  const posts = rawPosts.map((post, index) => {
    return {
      index: Number(post?.index || post?.order || index + 1),
      url: post?.url || post?.permalink || post?.link || '',
      shortcode: post?.shortcode || post?.code || '',
      caption: post?.caption || post?.content || post?.text || '',
      timestamp: post?.timestamp || post?.taken_at || post?.date || post?.publishedAt || '',
      mediaUrl: post?.mediaUrl || post?.media_url || post?.image || post?.thumbnail || post?.displayUrl || '',
      mediaType: post?.mediaType || post?.media_type || (post?.isVideo ? 'video' : (post?.url || '').includes('/reel/') ? 'video' : 'image'),
      videoUrl: post?.videoUrl || post?.video_url || '',
      mediaItems: Array.isArray(post?.mediaItems || post?.media_items || post?.media) ? (post.mediaItems || post.media_items || post.media) : []
    };
  });
  const username = source?.username || source?.user?.username || source?.profile?.username || extractUsername(activeUrl) || 'username';
  const profile = source?.profile && typeof source.profile === 'object' ? source.profile : source;
  return {
    username,
    profileUrl: source?.profileUrl || source?.profile_url || profile?.url || `https://www.instagram.com/${encodeURIComponent(username)}/`,
    avatar: source?.avatar || source?.profilePic || source?.profile_pic_url || profile?.avatar || profile?.profile_pic_url || '',
    fullName: source?.fullName || source?.full_name || profile?.full_name || '',
    biography: source?.biography || profile?.biography || '',
    externalUrl: source?.externalUrl || source?.external_url || profile?.external_url || '',
    isVerified: Boolean(source?.isVerified ?? source?.is_verified ?? profile?.is_verified),
    followers: source?.followers ?? source?.followerCount ?? source?.follower_count ?? profile?.followers ?? profile?.follower_count,
    following: source?.following ?? source?.followingCount ?? source?.following_count ?? profile?.following ?? profile?.following_count,
    posts,
    total: Number(source?.total ?? source?.postCount ?? posts.length),
    note: source?.note || source?.message || (Array.isArray(payload?.warnings) && payload.warnings.length ? payload.warnings.join(' ') : '')
  };
}

function extractUsername(value) {
  try {
    const parsed = new URL(value);
    const segment = parsed.pathname.split('/').filter(Boolean)[0];
    return segment || '';
  } catch { return ''; }
}

function setState(kind, title, message) {
  statePanel.hidden = false;
  statePanel.className = `state-panel ${kind}`;
  stateIcon.textContent = kind === 'error' ? '!' : kind === 'success' ? '✓' : 'i';
  stateTitle.textContent = title;
  stateMessage.textContent = message;
}

function clearState() {
  statePanel.hidden = true;
  statePanel.className = 'state-panel';
}

function setLoading(loading) {
  lookupButton.disabled = loading;
  refreshButton.disabled = loading;
  lookupButton.querySelector('.button-label').textContent = loading ? '读取中…' : '查看帖子';
}

function formatDate(value) {
  if (!value) return '日期未提供';
  const numericValue = typeof value === 'number' || /^\d+$/.test(String(value)) ? Number(value) : NaN;
  const date = Number.isFinite(numericValue)
    ? new Date(numericValue < 100000000000 ? numericValue * 1000 : numericValue)
    : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'short', day: 'numeric' }).format(date);
}

function renderAccount(data) {
  document.querySelector('#resultsTitle').textContent = `@${data.username}`;
  document.querySelector('#accountLink').href = data.profileUrl;
  document.querySelector('#postCount').textContent = data.total || data.posts.length;
  document.querySelector('#visibleCount').textContent = data.posts.length;
  const formatCount = (value) => {
    if (value === null || value === undefined || value === '') return '—';
    return new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(Number(value));
  };
  document.querySelector('#followerCount').textContent = formatCount(data.followers);
  document.querySelector('#followingCount').textContent = formatCount(data.following);
  const fullName = document.querySelector('#accountFullName');
  fullName.textContent = data.fullName || '';
  fullName.hidden = !data.fullName || data.fullName === data.username;
  const bio = document.querySelector('#accountBio');
  bio.textContent = normalizeDisplayText(data.biography);
  bio.hidden = !bio.textContent;
  const external = document.querySelector('#accountExternal');
  if (data.externalUrl) {
    external.href = data.externalUrl;
    external.textContent = data.externalUrl.replace(/^https?:\/\//, '').replace(/\/$/, '');
    external.hidden = false;
  } else external.hidden = true;
  document.querySelector('#accountVerified').hidden = !data.isVerified;
  const avatar = document.querySelector('#accountAvatar');
  avatar.replaceChildren();
  if (data.avatar) {
    const image = document.createElement('img');
    image.src = data.avatar;
    image.alt = `@${data.username}`;
    image.referrerPolicy = 'no-referrer';
    image.onerror = () => { avatar.textContent = '@'; };
    avatar.append(image);
  } else avatar.textContent = '@';
}

function renderPosts(data) {
  postList.replaceChildren();
  emptyResults.hidden = data.posts.length > 0;
  data.posts.forEach((post, index) => {
    const card = document.createElement('article');
    card.className = 'post-card';
    let mediaItems = (post.mediaItems.length ? post.mediaItems : [{
      type: post.mediaType,
      imageUrl: post.mediaUrl,
      videoUrl: post.videoUrl
    }]).map((item) => ({
      type: item?.type || item?.mediaType || 'image',
      imageUrl: item?.imageUrl || item?.image_url || item?.previewUrl || item?.preview_url || item?.mediaUrl || '',
      videoUrl: item?.videoUrl || item?.video_url || ''
    })).filter((item) => item.imageUrl || item.videoUrl);
    if (post.videoUrl && post.mediaType === 'video' && !mediaItems.some((item) => item.videoUrl)) {
      mediaItems = [{ type: 'video', imageUrl: post.mediaUrl, videoUrl: post.videoUrl }];
    }
    const media = mediaItems.length
      ? `<div class="media-stage">${mediaItems.map((item, mediaIndex) => {
          const isVideo = item.type === 'video' || Boolean(item.videoUrl);
          if (isVideo && item.videoUrl) {
            return `<video class="media-item${mediaIndex === 0 ? ' is-active' : ''}" data-media-index="${mediaIndex}" src="${escapeHtml(item.videoUrl)}"${item.imageUrl ? ` poster="${escapeHtml(item.imageUrl)}"` : ''} controls playsinline preload="metadata" referrerpolicy="no-referrer"></video>`;
          }
          return `<img class="media-item${mediaIndex === 0 ? ' is-active' : ''}" data-media-index="${mediaIndex}" src="${escapeHtml(item.imageUrl)}" alt="第 ${index + 1} 条帖子的第 ${mediaIndex + 1} 张图片" loading="lazy" referrerpolicy="no-referrer">`;
        }).join('')}</div>${mediaItems.length > 1 ? `<div class="media-controls" aria-label="帖子媒体切换"><button type="button" class="media-prev" aria-label="上一张媒体">‹</button><span class="media-counter">1 / ${mediaItems.length}</span><button type="button" class="media-next" aria-label="下一张媒体">›</button></div>` : ''}`
      : '<div class="media-placeholder">暂无预览</div>';
    const caption = renderTextBlock(post.caption, 'caption', '这条帖子没有公开正文。');
    const targetUrl = post.url || (post.shortcode ? `https://www.instagram.com/p/${encodeURIComponent(post.shortcode)}/` : '');
    card.innerHTML = `
      <div class="post-order">${String(post.index || index + 1).padStart(2, '0')}<small>POST</small></div>
      <div class="media-frame">${media}${post.mediaType === 'video' ? '<span class="media-badge">VIDEO</span>' : ''}</div>
      <div class="post-content">
        <div class="post-meta"><span class="post-date">${escapeHtml(formatDate(post.timestamp))}</span>${targetUrl ? `<a class="post-link" href="${escapeHtml(targetUrl)}" target="_blank" rel="noopener noreferrer">打开原帖 ↗</a>` : ''}</div>
        ${caption}
      </div>`;
    postList.append(card);
    card.querySelectorAll('.media-frame img, .media-frame video').forEach((mediaElement) => {
      mediaElement.addEventListener('error', () => {
        mediaElement.replaceWith(Object.assign(document.createElement('div'), { className: 'media-placeholder', textContent: '预览不可用' }));
      }, { once: true });
    });
    const items = [...card.querySelectorAll('.media-item')];
    const counter = card.querySelector('.media-counter');
    const setMedia = (nextIndex) => {
      const activeIndex = (nextIndex + items.length) % items.length;
      items.forEach((item, itemIndex) => item.classList.toggle('is-active', itemIndex === activeIndex));
      if (counter) counter.textContent = `${activeIndex + 1} / ${items.length}`;
      items[activeIndex]?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    };
    card.querySelector('.media-prev')?.addEventListener('click', () => setMedia(items.findIndex((item) => item.classList.contains('is-active')) - 1));
    card.querySelector('.media-next')?.addEventListener('click', () => setMedia(items.findIndex((item) => item.classList.contains('is-active')) + 1));
  });
  postList.querySelectorAll('.text-toggle').forEach((toggle) => {
    toggle.addEventListener('click', () => {
      const preview = toggle.closest('.text-preview');
      const expanded = preview.classList.toggle('is-expanded');
      preview.classList.toggle('is-collapsed', !expanded);
      toggle.setAttribute('aria-expanded', String(expanded));
      toggle.textContent = expanded ? '收起正文' : '展开全文';
    });
  });
}

async function lookup(url) {
  activeUrl = url;
  clearState();
  results.hidden = true;
  setLoading(true);
  try {
    const pinned = sortMode?.value === 'pinned';
    const response = await fetch(`/api/profile?url=${encodeURIComponent(url)}&pinned=${pinned ? 'true' : 'false'}`, { headers: { Accept: 'application/json' } });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.error) {
      const detail = typeof payload.error === 'object' ? payload.error.message : payload.error;
      throw new Error(detail || `服务器返回 ${response.status}`);
    }
    const data = normalizePayload(payload);
    renderAccount(data);
    renderPosts(data);
    document.querySelector('#resultNote').textContent = data.note || (pinned ? '按 Instagram 页面顺序排列，保留置顶优先级' : '按发布时间从新到旧排列');
    results.hidden = false;
    clearButton.hidden = false;
    setState('success', '读取完成', data.posts.length ? `已读取 ${data.posts.length} 条公开帖子。` : '页面可访问，但没有返回帖子数据。');
    results.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    setState('error', '读取失败', error.message || '无法访问服务端，请确认 Python 服务正在运行。');
  } finally { setLoading(false); }
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const url = input.value.trim();
  if (!url) { formHint.textContent = '请输入 Instagram 主页链接。'; formHint.style.color = 'var(--danger)'; input.focus(); return; }
  formHint.textContent = '支持公开账号主页、帖子和 Reels 链接';
  formHint.style.color = '';
  lookup(url);
});

clearButton.addEventListener('click', () => { input.value = ''; input.focus(); clearButton.hidden = true; results.hidden = true; clearState(); });
refreshButton.addEventListener('click', () => { if (activeUrl) lookup(activeUrl); });
