// "Selecionar placar" (Gerenciar e página do jogo - templates/tournaments/_pick_dialog.html): o botão
// [data-pick-score] abre a janela #selecionar-placar e busca os cards, já filtrados pelos dois jogadores
// do jogo e pela ISO do campeonato (tournaments.pick_scores). O "Usar este placar" de cada card é um
// formulário comum (tournaments.pick_score).
(function () {
  var dialog = document.getElementById('selecionar-placar');
  if (!dialog) return;
  var list = dialog.querySelector('[data-pick-list]');
  var game = dialog.querySelector('[data-pick-game]');
  var asked = 0;
  function message(text, error) {
    list.textContent = '';
    var p = document.createElement('p');
    p.className = error ? 'notice error' : 'muted';
    p.textContent = text;
    list.appendChild(p);
  }
  function load(src) {
    var mine = ++asked;  // só a última busca vale (abriu outro jogo antes de a anterior voltar)
    list.setAttribute('aria-busy', 'true');
    message('Buscando as partidas...');
    fetch(src, { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) { if (mine === asked) list.innerHTML = html; })
      .catch(function () { if (mine === asked) message('Não foi possível buscar as partidas. Tente de novo.', true); })
      .then(function () { if (mine === asked) list.removeAttribute('aria-busy'); });
  }
  document.addEventListener('click', function (e) {
    var button = e.target.closest('[data-pick-score]');
    if (!button) return;
    game.textContent = button.dataset.game || '';
    if (!dialog.open) dialog.showModal();
    load(button.dataset.pickScore);
  });
})();
