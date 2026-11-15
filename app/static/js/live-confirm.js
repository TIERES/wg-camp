// "Confirmar" nos cards do Ao vivo (live/_card.html, live._confirmation): confirma a
// partida por quem está logado sem sair da página (POST /ao-vivo/<id>/confirmar),
// mostra o toast "Obrigado por confirmar a partida A x B" e troca os botões pelo
// "Você confirmou", o link da página do jogo do campeonato ou do amistoso. Os
// cards se recarregam sozinhos (live-cards.js): o clique é tratado no documento.
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
      var box = button.closest('.live-confirm');
      var done = document.createElement('a');
      done.className = 'button secondary small live-confirmed';
      done.href = data.url;
      done.title = 'Você confirmou: ver a partida';
      done.setAttribute('aria-label', done.title);
      var icon = document.createElement('span');
      icon.className = 'live-confirm-ico';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = '✅';
      var text = document.createElement('span');
      text.className = 'live-confirm-text';
      text.textContent = 'Você confirmou';
      done.append(icon, text);
      if (box) box.replaceChildren(done);
    }).catch(function () {
      button.disabled = false;
      say('Sem conexão com o site: tente de novo.', 'error');
    });
  });
})();
