// Cards de partidas ao vivo (app/live.py): cada [data-live-src] recarrega os
// cards a cada poucos segundos. A seção [data-live-section] que o contém
// some quando não há nenhum (na página /ao-vivo, mostra o aviso [data-live-empty]).
(function () {
  var EVERY = 10000;
  var SWEEP = 2400;  // um ciclo do risco embaixo do tempo (site.css, live-sweep)
  // Todos os riscos na mesma fase, tirada do relógio: o card novo continua de onde o antigo estava.
  function phase(root) {
    var delay = -((Date.now() % SWEEP) / 1000) + 's';
    root.querySelectorAll('.live-clock.on').forEach(function (el) { el.style.setProperty('--live-phase', delay); });
  }
  phase(document);
  // Nome do time e nick que não cabem na coluna do card (celular): a fonte diminui até a
  // palavra mais longa caber inteira, até 70% do tamanho, em vez de quebrar no meio
  // ("MOROCC/O"). Abaixo disso, o overflow-wrap:anywhere do site.css quebra como antes.
  var MIN_SCALE = 0.7;
  function fitTeam(team) {
    var room = team.getBoundingClientRect().width;
    if (!room) return;  // card escondido: o ResizeObserver chama de novo quando aparecer
    team.querySelectorAll('strong, small').forEach(function (el) {
      el.style.fontSize = '';
      el.style.overflowWrap = 'normal';
      el.style.width = 'min-content';  // a largura da palavra mais longa
      var base = parseFloat(getComputedStyle(el).fontSize), size = base, w;
      // 1px de folga: com a palavra na largura exata da coluna, o arredondamento já quebra.
      for (var i = 0; i < 12 && (w = el.getBoundingClientRect().width) > room - 1 && size > base * MIN_SCALE; i++) {
        size = Math.max(base * MIN_SCALE, size * Math.min(0.97, (room - 1) / w));
        el.style.fontSize = size + 'px';
      }
      el.style.overflowWrap = '';
      el.style.width = '';
    });
  }
  // Refaz quando a coluna muda de largura (girar o celular, card que aparece no carrossel).
  // Só pela largura (a altura muda com a própria fonte) e fora do callback (requestAnimationFrame),
  // para não virar laço do ResizeObserver.
  function watchTeams(box) {
    var teams = box.querySelectorAll('.live-team');
    if (!window.ResizeObserver) { teams.forEach(fitTeam); return null; }
    var ro = new ResizeObserver(function (entries) {
      var changed = [];
      entries.forEach(function (e) {
        if (e.contentRect.width !== e.target._fitWidth) { e.target._fitWidth = e.contentRect.width; changed.push(e.target); }
      });
      if (changed.length) requestAnimationFrame(function () { changed.forEach(fitTeam); });
    });
    teams.forEach(function (t) { ro.observe(t); });
    return ro;
  }
  document.querySelectorAll('[data-live-src]').forEach(function (box) {
    var section = box.closest('[data-live-section]');
    var empty = section && section.querySelector('[data-live-empty]');
    var watcher = watchTeams(box);
    function show(html) {
      var x = box.scrollLeft;  // no carrossel (_carousel.html), continua no mesmo card
      if (watcher) watcher.disconnect();  // os cards antigos saem da página
      box.innerHTML = html;
      watcher = watchTeams(box);
      box.scrollLeft = x;
      phase(box);
      var any = box.children.length > 0;
      if (empty) empty.hidden = any;
      else if (section) section.hidden = !any;
    }
    function load() {
      fetch(box.dataset.liveSrc, { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (html) { if (html !== null) show(html.trim()); })
        .catch(function () {});
    }
    if (!empty) load();  // na inicial/campeonato os cards vêm só pelo fetch
    setInterval(load, EVERY);
  });
})();
