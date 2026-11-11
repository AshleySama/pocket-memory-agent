(() => {
  'use strict';
  const frame = document.querySelector('#video-player');
  const video = document.querySelector('#product-video');
  if (!frame || !video) return;
  const controls = frame.querySelector('.player-controls');
  const play = frame.querySelector('.player-play');
  const seek = frame.querySelector('.player-seek');
  const mute = frame.querySelector('.player-mute');
  const volume = frame.querySelector('.player-volume');
  const speed = frame.querySelector('.speed-toggle');
  const menu = frame.querySelector('.speed-menu');
  const fullscreen = frame.querySelector('.player-fullscreen');
  const status = frame.querySelector('#video-status');
  const speedButtons = [...menu.querySelectorAll('[data-speed]')];
  const rates = new Set([0.75, 1, 1.5, 2, 3]);
  let selectedRate = video.playbackRate;
  const timeText = value => {
    if (!Number.isFinite(value)) return '--:--';
    const seconds = Math.max(0, Math.floor(value));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  };
  const updateTime = () => {
    frame.querySelector('[data-time]').textContent = timeText(video.currentTime);
    frame.querySelector('[data-duration]').textContent = timeText(video.duration);
    seek.disabled = !Number.isFinite(video.duration) || video.duration <= 0;
    if (!seek.disabled) seek.value = String(video.currentTime / video.duration * 100);
    seek.setAttribute('aria-valuetext', `${timeText(video.currentTime)}，共 ${timeText(video.duration)}`);
  };
  const updatePlay = () => {
    play.setAttribute('aria-label', video.paused ? '播放' : '暂停');
    play.querySelector('.play-symbol').toggleAttribute('hidden', !video.paused);
    play.querySelector('.pause-symbol').toggleAttribute('hidden', video.paused);
  };
  const togglePlay = async () => {
    if (!video.paused) { video.pause(); return; }
    try { await video.play(); } catch (error) {
      if (error.name !== 'AbortError') { status.textContent = '视频暂时无法播放，请稍后重试。'; status.hidden = false; }
    }
  };
  const closeMenu = (restoreFocus = false) => {
    menu.hidden = true;
    speed.setAttribute('aria-expanded', 'false');
    if (restoreFocus) speed.focus();
  };
  const updateSpeed = () => {
    selectedRate = video.playbackRate;
    speed.textContent = `${selectedRate}×`;
    speed.setAttribute('aria-label', `播放速度，当前 ${selectedRate} 倍`);
    speedButtons.forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.speed) === selectedRate)));
  };
  const updateVolume = () => {
    const silent = video.muted || video.volume === 0;
    mute.setAttribute('aria-label', silent ? '取消静音' : '静音');
    mute.setAttribute('aria-pressed', String(silent));
    mute.querySelector('.sound-waves').toggleAttribute('hidden', silent);
    mute.querySelector('.muted-cross').toggleAttribute('hidden', !silent);
    volume.value = String(video.muted ? 0 : video.volume);
  };
  play.addEventListener('click', togglePlay);
  video.addEventListener('click', togglePlay);
  seek.addEventListener('input', () => { if (!seek.disabled) { video.currentTime = Number(seek.value) / 100 * video.duration; updateTime(); } });
  mute.addEventListener('click', () => { video.muted = !video.muted; if (!video.muted && !video.volume) video.volume = 1; });
  volume.addEventListener('input', () => { video.volume = Number(volume.value); video.muted = video.volume === 0; });
  speed.addEventListener('click', () => {
    const opening = menu.hidden;
    menu.hidden = !opening;
    speed.setAttribute('aria-expanded', String(opening));
    if (opening) (speedButtons.find(button => button.getAttribute('aria-pressed') === 'true') || speedButtons[0]).focus();
  });
  speedButtons.forEach(button => button.addEventListener('click', () => {
    const rate = Number(button.dataset.speed);
    if (!rates.has(rate)) return;
    video.defaultPlaybackRate = rate;
    video.playbackRate = rate;
    updateSpeed(); closeMenu(true);
  }));
  menu.addEventListener('keydown', event => {
    const index = speedButtons.indexOf(document.activeElement);
    if (event.key === 'Escape') { event.preventDefault(); closeMenu(true); }
    else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); speedButtons[(index + (event.key === 'ArrowDown' ? 1 : -1) + speedButtons.length) % speedButtons.length].focus();
    }
  });
  document.addEventListener('pointerdown', event => { if (!event.target.closest('.player-speed')) closeMenu(); });
  frame.addEventListener('focusout', event => { if (!frame.querySelector('.player-speed').contains(event.relatedTarget)) closeMenu(); });
  fullscreen.addEventListener('click', async () => {
    try { if (document.fullscreenElement) await document.exitFullscreen(); else await frame.requestFullscreen(); }
    catch (_) { status.textContent = '当前浏览器未允许全屏，可继续在页面中观看。'; status.hidden = false; }
  });
  document.addEventListener('fullscreenchange', () => { fullscreen.setAttribute('aria-label', document.fullscreenElement === frame ? '退出全屏' : '全屏'); closeMenu(); });
  if (!document.fullscreenEnabled) fullscreen.hidden = true;
  video.addEventListener('loadedmetadata', () => { video.playbackRate = selectedRate; updateTime(); });
  ['timeupdate', 'durationchange'].forEach(event => video.addEventListener(event, updateTime));
  ['play', 'pause', 'ended'].forEach(event => video.addEventListener(event, updatePlay));
  video.addEventListener('volumechange', updateVolume);
  video.addEventListener('ratechange', updateSpeed);
  video.addEventListener('playing', () => { status.hidden = true; });
  const showError = () => { status.textContent = '视频暂时无法加载，请稍后刷新重试。'; status.hidden = false; };
  video.addEventListener('error', showError);
  video.querySelectorAll('source').forEach(source => source.addEventListener('error', showError));
  updatePlay(); updateTime(); updateVolume(); updateSpeed();
  video.controls = false;
  controls.hidden = false;
})();
