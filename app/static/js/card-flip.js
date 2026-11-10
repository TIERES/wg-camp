// Card do amistoso: a bola do título troca o conteúdo - os jogos sobem
// (recolhem) e os artilheiros e as assistências descem, e vice-versa
// (.friendly-slide, a animação no CSS). O lado escondido fica inert.
document.addEventListener('click', function (e) {
  var button = e.target.closest('[data-card-flip]');
  if (!button) return;
  var card = button.closest('.friendly-card'), slide = card && card.querySelector('.friendly-slide');
  if (!slide) return;
  var back = !slide.classList.contains('flipped');
  slide.classList.toggle('flipped', back);
  Array.prototype.forEach.call(slide.children, function (pane) {
    pane.inert = pane.classList.contains('back') !== back;
  });
  button.setAttribute('aria-pressed', String(back));
});
