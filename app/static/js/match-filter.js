// Aba Jogos do campeonato: filtro por jogador ou time (entry_picker, [data-match-filter]).
// Mostra só as linhas de jogo (.match-line[data-entries]) da inscrição escolhida; some a
// rodada e a fase sem nenhum jogo dela. ?jogador=<inscrição> no endereço guarda a escolha.
(function () {
  var box = document.querySelector('[data-match-filter]');
  var area = document.querySelector('[data-match-filter-list]');
  if (!box || !area) return;
  var input = box.querySelector('[role=combobox]'), hidden = box.querySelector('[data-user-id]');
  var clear = box.querySelector('[data-match-filter-clear]');
  var empty = document.querySelector('[data-match-filter-empty]');

  function apply() {
    var id = hidden.value;
    var shown = 0;
    area.querySelectorAll('.match-line[data-entries]').forEach(function (line) {
      var row = line.closest('.match-line-live') || line;
      row.hidden = !!id && line.dataset.entries.split(' ').indexOf(id) < 0;
      if (!row.hidden) shown++;
    });
    // A primeira linha à mostra de cada card fica sem a borda de cima.
    area.querySelectorAll('.round-card').forEach(function (card) {
      var first = true;
      Array.prototype.forEach.call(card.children, function (row) {
        row.classList.toggle('first-shown', first && !row.hidden);
        if (!row.hidden) first = false;
      });
    });
    area.querySelectorAll('.round-legs').forEach(function (rnd) {
      var any = Array.prototype.some.call(rnd.querySelectorAll('.match-line[data-entries]'), function (line) {
        return !(line.closest('.match-line-live') || line).hidden;
      });
      rnd.hidden = !!id && !any;
    });
    area.querySelectorAll('section.stage').forEach(function (stage) {
      stage.hidden = !!id && !stage.querySelector('.round-legs:not([hidden])');
    });
    clear.hidden = !id;
    if (empty) empty.hidden = !id || shown > 0;
    var url = new URL(window.location.href);
    if (id) url.searchParams.set('jogador', id); else url.searchParams.delete('jogador');
    window.history.replaceState(null, '', url);
  }

  input.addEventListener('change', apply);
  input.addEventListener('input', function () { if (!input.value.trim()) { hidden.value = ''; apply(); } });
  clear.addEventListener('click', function () { input.value = ''; hidden.value = ''; apply(); input.focus(); });
  if (hidden.value) apply();
})();
