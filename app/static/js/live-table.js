// Aba Tabela do campeonato (tournaments/show.html): a cada poucos segundos,
// "Ao vivo" nos grupos com jogo rolando e o placar ao lado dos dois times
// (app/live.py tournament_live: verde ganhando, vermelho perdendo, cinza empatando).
(function () {
  var EVERY = 10000;
  document.querySelectorAll('[data-live-table]').forEach(function (box) {
    function apply(data) {
      var groups = (data.groups || []).map(String);
      var entries = data.entries || {};
      box.querySelectorAll('[data-live-group]').forEach(function (group) {
        var label = group.querySelector('.standings-live');
        if (label) label.hidden = groups.indexOf(group.dataset.liveGroup) < 0;
      });
      box.querySelectorAll('tr[data-entry]').forEach(function (row) {
        var info = entries[row.dataset.entry];
        var cell = row.querySelector('.name-col');
        var tag = cell && cell.querySelector('.live-mini');
        if (!cell) return;
        if (!info) {
          if (tag) tag.remove();
          return;
        }
        if (!tag) {
          tag = document.createElement('span');
          tag.title = 'Jogando agora';
          cell.appendChild(tag);
        }
        tag.className = 'live-mini ' + info.result;
        tag.textContent = info.score;
      });
    }
    function load() {
      fetch(box.dataset.liveTable, { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) { if (data) apply(data); })
        .catch(function () {});
    }
    setInterval(load, EVERY);
  });
})();
