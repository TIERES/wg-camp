// Convites para campeonato (_invite_picker.html): marcar todos os amigos, os jogadores
// de um campeonato anterior, limpar e buscar pelo nome. Convidados e inscritos ficam
// marcados e travados (data-locked, fora do formulário); o botão data-invite-submit
// só habilita quando há alguém novo marcado.
(function () {
  function plain(s) { return (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase(); }

  document.querySelectorAll('.invite-picker').forEach(function (picker) {
    if (picker.dataset.ready) return;
    picker.dataset.ready = '1';
    var boxes = Array.prototype.slice.call(picker.querySelectorAll('input[name="invite_ids"]'));
    var count = picker.querySelector('.invite-count');
    var submit = picker.querySelector('[data-invite-submit]');
    picker.querySelectorAll('input[data-locked]').forEach(function (b) {
      b.addEventListener('click', function (e) { e.preventDefault(); });
    });

    function update() {
      var n = boxes.filter(function (b) { return b.checked; }).length;
      if (count) count.textContent = n ? n + (n === 1 ? ' jogador marcado' : ' jogadores marcados') : '';
      if (submit) submit.disabled = !n;
    }
    function mark(test) { boxes.forEach(function (b) { if (!b.disabled && test(b)) b.checked = true; }); update(); }

    var friends = picker.querySelector('[data-invite-friends]');
    if (friends) friends.addEventListener('click', function () {
      mark(function (b) { return b.closest('.invite-option').dataset.friend === '1'; });
    });
    var select = picker.querySelector('[data-invite-tournament]');
    if (select) select.addEventListener('change', function () {
      var ids = this.value ? this.value.split(',') : [];
      mark(function (b) { return ids.indexOf(b.value) >= 0; });
      this.value = '';
    });
    var clear = picker.querySelector('[data-invite-clear]');
    if (clear) clear.addEventListener('click', function () { boxes.forEach(function (b) { b.checked = false; }); update(); });
    var filter = picker.querySelector('[data-invite-filter]');
    if (filter) {
      filter.addEventListener('keydown', function (e) { if (e.key === 'Enter') e.preventDefault(); });
      filter.addEventListener('input', function () {
        var q = plain(this.value.trim());
        picker.querySelectorAll('.invite-option').forEach(function (o) {
          o.hidden = !!q && plain(o.dataset.name).indexOf(q) < 0;
        });
      });
    }
    boxes.forEach(function (b) { b.addEventListener('change', update); });
    update();
  });
})();
