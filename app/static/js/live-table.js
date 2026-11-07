// Aba Tabela do campeonato em andamento (tournaments/show.html): a cada poucos
// segundos recarrega as tabelas de grupos/pontos corridos (tournaments.live_tables)
// - "Ao vivo" no grupo com jogo rolando, o placar ao lado dos dois times e a
// classificação com os placares ainda não confirmados (app/live.py, store.group_tables).
(function () {
  var EVERY = 10000;
  var SWEEP = 2400;  // um ciclo do risco embaixo do "Ao vivo" (site.css, live-sweep)
  // Mesma fase para todos, tirada do relógio: o risco não salta quando a tabela recarrega.
  function phase(root) {
    var delay = -((Date.now() % SWEEP) / 1000) + 's';
    root.querySelectorAll('.standings-live').forEach(function (el) { el.style.setProperty('--live-phase', delay); });
  }
  phase(document);  // também o "Ao vivo" da aba Jogos
  var blocks = document.querySelectorAll('[data-live-stage]');
  if (!blocks.length) return;
  var url = blocks[0].dataset.liveUrl;
  function load() {
    if (document.hidden) return;
    fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (html === null) return;
        var fresh = document.createElement('div');
        fresh.innerHTML = html;
        fresh.querySelectorAll('[data-live-stage]').forEach(function (block) {
          var old = document.querySelector('.groups[data-live-stage="' + block.dataset.liveStage + '"]');
          if (old) old.innerHTML = block.innerHTML;
        });
        phase(document);
      })
      .catch(function () {});
  }
  setInterval(load, EVERY);
})();
