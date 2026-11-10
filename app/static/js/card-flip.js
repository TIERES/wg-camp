// Card do amistoso: a bola do título (e o "Voltar aos jogos" do verso) gira o
// card para os artilheiros e as assistências - o mesmo giro do ida/volta
// (.tie-flip). O lado escondido fica inert.
document.addEventListener('click', function (e) {
  var button = e.target.closest('[data-card-flip]');
  if (!button) return;
  var card = button.closest('.friendly-card'), flip = card && card.querySelector('.friendly-flip');
  if (!flip) return;
  var back = !flip.classList.contains('flipped');
  // O verso fica por cima da frente (position absolute): o card cresce até a altura dele.
  var backFace = flip.querySelector(':scope > .back');
  flip.style.minHeight = back && backFace ? backFace.offsetHeight + 'px' : '';
  flip.classList.toggle('flipped', back);
  Array.prototype.forEach.call(flip.children, function (face) {
    face.inert = face.classList.contains('back') !== back;
  });
  card.querySelectorAll('.flip-ball').forEach(function (ball) { ball.setAttribute('aria-pressed', String(back)); });
});
