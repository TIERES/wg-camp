// Pênaltis do mata-mata: os campos só aparecem quando o placar digitado empata o
// confronto (somando os outros jogos já confirmados, em data-agg="mandante,visitante").
(function () {
  document.querySelectorAll('.pens[data-agg]').forEach(function (pens) {
    var form = pens.closest('form');
    var home = form.elements.home_goals, away = form.elements.away_goals;
    var agg = pens.dataset.agg.split(',').map(Number);
    var fields = pens.querySelectorAll('input');
    function refresh() {
      var draw = home.value !== '' && away.value !== '' && agg[0] + Number(home.value) === agg[1] + Number(away.value);
      pens.hidden = !draw;
      fields.forEach(function (el) { el.disabled = !draw; el.required = draw; });
    }
    home.addEventListener('input', refresh);
    away.addEventListener('input', refresh);
    refresh();
  });
})();
