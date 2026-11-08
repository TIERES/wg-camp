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
  document.querySelectorAll('[data-live-src]').forEach(function (box) {
    var section = box.closest('[data-live-section]');
    var empty = section && section.querySelector('[data-live-empty]');
    function show(html) {
      var x = box.scrollLeft;  // no carrossel (_carousel.html), continua no mesmo card
      box.innerHTML = html;
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
