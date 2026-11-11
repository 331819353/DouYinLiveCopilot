// 滚动提词：速度按“字/分钟”换算成像素，悬停缓停，移开后缓起。
// 用 requestAnimationFrame + transform 实现，不用 CSS 动画，方便变速和暂停续播。

const READ_LINE = 0.35;   // 阅读线在视口高度 35% 处
const STOP_TAU = 0.15;    // 缓停时间常数（秒）
const START_TAU = 0.3;    // 缓起时间常数（秒）
const RESUME_DELAY = 300; // 鼠标移开后多久开始恢复（毫秒）

export class Scroller {
  constructor(viewport, track, { cpm = 240, onCurrent, onVisible, onPause } = {}) {
    this.viewport = viewport;
    this.track = track;
    this.cpm = cpm;
    this.onCurrent = onCurrent;
    this.onVisible = onVisible;
    this.onPause = onPause;
    this.offset = 0;
    this.factor = 1;        // 0 = 停住，1 = 全速
    this.hover = false;
    this.locked = false;
    this.holds = new Set(); // 其他原因的暂停，例如正在看某张卡片
    this.resumeAt = 0;
    this.paras = [];        // { el, id, top, bottom }，相对 track 的位置
    this.currentId = null;
    this.visible = new Set();
    this.last = performance.now();
    this.lastPauseState = null;

    viewport.addEventListener("pointerenter", () => { this.hover = true; });
    viewport.addEventListener("pointerleave", () => {
      this.hover = false;
      this.resumeAt = performance.now() + RESUME_DELAY;
    });
    // 暂停时（悬停就是暂停）可以用滚轮前后翻看
    viewport.addEventListener("wheel", (e) => {
      if (!this.paused()) return;
      e.preventDefault();
      this.offset = this.clamp(this.offset + e.deltaY);
      this.apply();
    }, { passive: false });
    new ResizeObserver(() => this.layout()).observe(viewport);

    this.io = new IntersectionObserver((entries) => {
      for (const en of entries) {
        const id = en.target.dataset.id;
        if (en.intersectionRatio >= 0.5) this.visible.add(id); else this.visible.delete(id);
      }
      this.onVisible?.([...this.visible]);
    }, { root: viewport, threshold: [0, 0.5, 1] });

    requestAnimationFrame((t) => this.tick(t));
  }

  setParagraphs(elements) {
    this.io.disconnect();
    this.paras = elements.map((el) => ({ el, id: el.dataset.id, top: 0, bottom: 0 }));
    for (const p of this.paras) this.io.observe(p.el);
    this.layout();
    this.apply();
  }

  // 重新测量：每像素对应多少字，以及每段的位置。字号或窗口变化时调用。
  layout() {
    const first = this.paras[0]?.el;
    if (!first) return;
    const probe = document.createElement("span");
    probe.textContent = "字".repeat(40);
    probe.style.cssText = "position:absolute;visibility:hidden;white-space:nowrap";
    first.appendChild(probe);
    const charW = probe.getBoundingClientRect().width / 40;
    probe.remove();
    const style = getComputedStyle(first);
    const lineH = parseFloat(style.lineHeight);
    const width = first.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const charsPerLine = Math.max(1, width / charW);
    this.pxPerChar = lineH / charsPerLine;
    for (const p of this.paras) {
      p.top = p.el.offsetTop;
      p.bottom = p.top + p.el.offsetHeight;
    }
  }

  get pxPerSec() { return (this.cpm / 60) * (this.pxPerChar || 0); }

  paused() { return this.hover || this.locked || this.holds.size > 0; }

  pauseReason() {
    if (this.locked) return "已锁定暂停，按空格继续";
    if (this.holds.size) return "查看回复卡片中";
    if (this.hover) return "悬停暂停";
    return null;
  }

  toggleLock() { this.locked = !this.locked; if (!this.locked) this.resumeAt = performance.now(); }
  hold(key) { this.holds.add(key); }
  release(key) {
    if (this.holds.delete(key) && !this.holds.size) this.resumeAt = performance.now() + RESUME_DELAY;
  }
  setSpeed(cpm) { this.cpm = Math.min(400, Math.max(120, cpm)); }

  clamp(v) {
    const max = this.track.scrollHeight - this.viewport.clientHeight;
    return Math.min(Math.max(0, v), Math.max(0, max));
  }

  apply() {
    this.track.style.transform = `translate3d(0, ${-this.offset}px, 0)`;
    this.updateCurrent();
  }

  updateCurrent() {
    // 阅读线在 track 坐标系里的位置；不读布局，避免每帧触发回流
    const y = this.offset + this.viewport.clientHeight * READ_LINE;
    let current = null;
    for (const p of this.paras) {
      if (p.top <= y) current = p;
      p.el.classList.toggle("read", p.bottom < y);
    }
    for (const p of this.paras) p.el.classList.toggle("current", p === current);
    const id = current?.id ?? null;
    if (id !== this.currentId) {
      this.currentId = id;
      this.onCurrent?.(id);
    }
  }

  tick(now) {
    const dt = Math.min(0.1, (now - this.last) / 1000);
    this.last = now;
    const target = !this.paused() && now >= this.resumeAt ? 1 : 0;
    const tau = target < this.factor ? STOP_TAU : START_TAU;
    this.factor += (target - this.factor) * (1 - Math.exp(-dt / tau));
    if (target === 0 && this.factor < 0.01) this.factor = 0;
    if (target === 1 && this.factor > 0.99) this.factor = 1;
    if (this.factor > 0) {
      this.offset = this.clamp(this.offset + this.pxPerSec * this.factor * dt);
      this.apply();
    }
    const reason = this.factor === 0 ? this.pauseReason() : null;
    if (reason !== this.lastPauseState) {
      this.lastPauseState = reason;
      this.onPause?.(reason);
    }
    requestAnimationFrame((t) => this.tick(t));
  }
}
