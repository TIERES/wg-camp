// Ida e volta: os botoes Ida/Volta giram o card (chave) ou a lista de jogos
// (aba Jogos, Gerenciar) para o jogo escolhido. O lado escondido fica inert.
document.addEventListener('click', function (e) {
  var button = e.target.closest('.leg-toggle button');
  if (!button) return;
  var box = button.closest('.tie-box, .leg-box'), back = button.dataset.leg === '1';
  if (!box) return;  // o Gols/Estatísticas do card do Ao vivo (live-cards.js) usa o mesmo visual
  box.querySelectorAll(':scope > .leg-toggle button').forEach(function (b) { b.setAttribute('aria-pressed', String(b === button)); });
  var flip = box.querySelector(':scope > .tie-flip');
  flip.classList.toggle('flipped', back);
  Array.prototype.forEach.call(flip.children, function (face) {
    face.inert = face.classList.contains('back') !== back;
  });
});
