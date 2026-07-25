(function() {
'use strict';

const SPA = {
  init() {
    history.replaceState({ url: window.location.href }, '', window.location.href);
    this.bindNavigation();
    this.bindForms();
    this.bindPopState();
  },

  bindNavigation() {
    document.addEventListener('click', (e) => {
      const link = e.target.closest('a[data-nav]');
      if (!link) return;
      const href = link.getAttribute('href');
      if (!href || href === '#') return;
      if (href.startsWith('http') || href.startsWith('//') || link.hasAttribute('target')) return;
      e.preventDefault();
      this.navigate(href);
    });
  },

  bindForms() {
    document.addEventListener('submit', (e) => {
      const form = e.target;
      if (form.getAttribute('data-spa') !== 'true') return;
      e.preventDefault();
      this.submitForm(form);
    });
  },

  bindPopState() {
    window.addEventListener('popstate', (e) => {
      if (e.state && e.state.url) {
        this._navigateFetch(e.state.url, { replace: true });
      }
    });
  },

  navigate(url) {
    if (url === window.location.pathname + window.location.search) return;
    this._navigateFetch(url, {});
  },

  async _navigateFetch(url, opts) {
    this.showLoading();
    try {
      const resp = await fetch(url, { headers: { 'X-SPA': '1' } });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const html = await resp.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const content = doc.getElementById('spa-content');
      if (!content) { window.location.href = url; return; }

      const target = document.getElementById('spa-content');
      target.innerHTML = content.innerHTML;
      document.title = doc.title;
      this._updateActiveNav(url);
      this._updatePageCss(doc);
      this._execScripts(target);
      if (!opts.replace) history.pushState({ url }, '', url);
      if (window.pageInit) window.pageInit();
      window.dispatchEvent(new CustomEvent('spa-navigated', { detail: { url } }));
    } catch (e) {
      console.error('SPA navigate error:', e);
      window.location.href = url;
    } finally {
      this.hideLoading();
    }
  },

  async submitForm(form) {
    this.showLoading();
    const formData = new FormData(form);
    const url = form.action || window.location.href;
    const method = (form.method || 'GET').toUpperCase();
    try {
      const resp = await fetch(url, {
        method,
        headers: method === 'POST' ? {} : {},
        body: method === 'POST' ? formData : null
      });
      if (!resp.ok && resp.status !== 302) {
        const text = await resp.text();
        const doc = new DOMParser().parseFromString(text, 'text/html');
        const content = doc.getElementById('spa-content');
        if (content) {
          document.getElementById('spa-content').innerHTML = content.innerHTML;
          document.title = doc.title;
          this._execScripts(document.getElementById('spa-content'));
          if (window.pageInit) window.pageInit();
        }
        return;
      }
      const html = await resp.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const content = doc.getElementById('spa-content');
      if (!content) { window.location.href = url; return; }
      document.getElementById('spa-content').innerHTML = content.innerHTML;
      document.title = doc.title;
      this._updateActiveNav(url);
      this._updatePageCss(doc);
      this._execScripts(document.getElementById('spa-content'));
      if (window.pageInit) window.pageInit();
      window.dispatchEvent(new CustomEvent('spa-navigated', { detail: { url } }));
    } catch (e) {
      console.error('SPA form error:', e);
    } finally {
      this.hideLoading();
    }
  },

  _execScripts(container) {
    container.querySelectorAll('script').forEach(old => {
      const s = document.createElement('script');
      if (old.src) { s.src = old.src; s.async = false; }
      else { s.textContent = old.textContent; }
      old.replaceWith(s);
    });
  },

  _updatePageCss(doc) {
    const newCss = doc.getElementById('page-css');
    const curCss = document.getElementById('page-css');
    if (newCss && curCss) {
      curCss.textContent = newCss.textContent;
    }
  },

  _updateActiveNav(url) {
    const path = url.split('?')[0].split('#')[0];
    document.querySelectorAll('.nav-link[data-nav]').forEach(link => {
      const href = link.getAttribute('href');
      const active = href === path || (href !== '/' && path.startsWith(href));
      link.classList.toggle('active', active);
    });
  },

  showLoading() {
    const el = document.getElementById('spa-loading');
    if (el) el.classList.add('show');
  },

  hideLoading() {
    const el = document.getElementById('spa-loading');
    if (el) el.classList.remove('show');
  }
};

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => SPA.init());
} else {
  SPA.init();
}

window.SPA = SPA;

})();
