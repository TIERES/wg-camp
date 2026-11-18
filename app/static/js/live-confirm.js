// "Confirmar" nos cards do Ao vivo (live/_card.html, live._confirmation): confirma a
// partida por quem está logado sem sair da página (POST /ao-vivo/<id>/confirmar),
// mostra o toast "Obrigado por confirmar a partida A x B", tira Contestar e
// Confirmar (o .live-confirm da linha de botões; os outros, como o Excluir, ficam)
// e põe o selo verde "TIERES confirmou" embaixo do status. Os cards se recarregam
// sozinhos (live-cards.js): o clique é tratado no documento.
(function () {
  function say(message, kind) {
    if (window.toast) window.toast(message, kind);
  }

  document.addEventListener('click', function (e) {
    var button = e.target.closest('[data-live-confirm]');
    if (!button || button.disabled) return;
    e.preventDefault();
    button.disabled = true;
    var body = new FormData();
    body.append('csrf_token', button.getAttribute('data-csrf') || '');
    fetch(button.getAttribute('data-live-confirm'), {
      method: 'POST', body: body, credentials: 'same-origin', headers: { Accept: 'application/json' }
    }).then(function (response) {
      return response.json().catch(function () { return { ok: 0, message: 'Não deu para confirmar agora: recarregue a página.' }; });
    }).then(function (data) {
      if (!data.ok) {
        button.disabled = false;
        say(data.message || 'Não deu para confirmar agora.', 'error');
        return;
      }
      say(data.message, 'success');
      // Sem os botões: os selos verdes "TIERES confirmou" embaixo do status, um por
      // jogador que já confirmou (data.confirmed_by).
      var card = button.closest('.live-card');
      var box = button.closest('.live-confirm');
      var row = button.closest('.live-actions');
      if (box) box.remove();
      if (row && !row.querySelector('.live-act')) row.remove();
      var status = card && card.querySelector('.live-status');
      if (status && data.confirmed_by && data.confirmed_by.length) {
        status.querySelectorAll('.live-confirmed-by').forEach(function (old) { old.remove(); });
        var before = status.querySelector('.live-deleted-by');
        data.confirmed_by.forEach(function (name) {
          var badge = document.createElement('span');
          badge.className = 'badge live-confirmed-by';
          badge.title = name + ' confirmou o placar desta partida';
          badge.textContent = name + ' confirmou';
          status.insertBefore(badge, before);
        });
      }
    }).catch(function () {
      button.disabled = false;
      say('Sem conexão com o site: tente de novo.', 'error');
    });
  });
})();
