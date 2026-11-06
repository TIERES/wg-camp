// Toasts: as mensagens do sistema (flash do servidor e validações do JS).
// O servidor já desenha os toasts no #toasts (base.html); aqui eles ganham o
// tempo para sumir, proporcional ao tamanho do texto, mostrado como barra de
// progresso. Passar o mouse (ou focar o botão de fechar) pausa a barra.
// Sem JS os toasts ficam na tela, sem barra.
// Uso no JS: toast('Mensagem', 'error' | 'success' | 'info').
(function () {
  var MIN_MS = 4000, MAX_MS = 15000;
  var ICONS = {
    error: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5h.01"/>',
    success: '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8L16.5 9.5"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5h.01"/>'
  };

  function duration(el) {
    var length = (el.querySelector('.toast-text').textContent || '').trim().length;
    var ms = 3000 + length * 60;  // ~ tempo de leitura: 3 s + 60 ms por letra
    if (el.classList.contains('error')) ms *= 1.3;  // erro fica um pouco mais
    return Math.round(Math.min(MAX_MS, Math.max(MIN_MS, ms)));
  }

  function close(el) {
    if (el.classList.contains('closing')) return;
    el.classList.add('closing');
    setTimeout(function () { el.remove(); }, 250);
  }

  function arm(el) {
    if (el.dataset.armed) return;
    el.dataset.armed = '1';
    el.style.setProperty('--toast-ms', duration(el) + 'ms');
    el.querySelector('.toast-close').addEventListener('click', function () { close(el); });
    el.querySelector('.toast-bar').addEventListener('animationend', function () { close(el); });
    el.classList.add('armed');
  }

  function stack() {
    var box = document.getElementById('toasts');
    if (!box) {
      box = document.createElement('div');
      box.id = 'toasts';
      box.className = 'toasts';
      document.body.appendChild(box);
    }
    return box;
  }

  window.toast = function (message, kind) {
    kind = ICONS[kind] ? kind : 'info';
    var el = document.createElement('div');
    el.className = 'toast ' + kind;
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    el.innerHTML = '<svg class="toast-icon" viewBox="0 0 24 24" aria-hidden="true">' + ICONS[kind] + '</svg>' +
      '<p class="toast-text"></p>' +
      '<button type="button" class="toast-close" aria-label="Fechar mensagem">&times;</button>' +
      '<span class="toast-bar" aria-hidden="true"></span>';
    el.querySelector('.toast-text').textContent = message;
    stack().appendChild(el);
    arm(el);
    return el;
  };

  document.querySelectorAll('#toasts .toast').forEach(arm);
})();
