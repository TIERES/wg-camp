// Barra de progresso do envio para o archive.org. Cada
// <div class="archive-progress" data-archive-status="URL"> consulta a URL (JSON
// de tournament/iso.status ou admin.file_archive_status) a cada 2 s enquanto o
// arquivo está sendo enviado ou conferido, e para quando termina.
//   sending   -> barra com o percentual enviado
//   verifying -> barra "andando": o archive.org confere o arquivo (minutos)
//   done      -> pronto
//   error     -> mensagem do erro
//   local     -> só neste servidor (sem envio em andamento)
(function () {
  var TEXT = {
    sending: function (s) { return 'Enviando para o archive.org... ' + s.progress + '%'; },
    verifying: function () { return 'Enviado! O archive.org está conferindo o arquivo (pode levar alguns minutos)...'; },
    done: function () { return 'No archive.org: pronto para download.'; },
    error: function (s) { return 'Erro no envio para o archive.org: ' + s.error; },
    local: function (s) {
      return s.configured ? 'Ainda não enviada para o archive.org.'
        : 'O archive.org não está configurado neste servidor: a ISO fica só aqui.';
    },
    none: function () { return ''; }
  };

  function setup(box) {
    var bar = box.querySelector('progress');
    var label = box.querySelector('.archive-progress-text');
    if (!bar) {
      bar = document.createElement('progress');
      bar.max = 100;
      box.appendChild(bar);
    }
    if (!label) {
      label = document.createElement('span');
      label.className = 'archive-progress-text';
      box.appendChild(label);
    }
    var started = false;

    function render(s) {
      box.dataset.state = s.state;
      box.hidden = s.state === 'none';
      bar.hidden = !(s.state === 'sending' || s.state === 'verifying' || s.state === 'done');
      if (s.state === 'verifying') bar.removeAttribute('value');  // indeterminada
      else bar.value = s.state === 'done' ? 100 : (s.progress || 0);
      label.textContent = (TEXT[s.state] || TEXT.none)(s);
      // Terminou depois de acompanhar na tela: avisa (a página ainda mostra a situação antiga).
      if (started && (s.state === 'done' || s.state === 'error')) {
        if (window.toast) toast(s.state === 'done' ? 'ISO enviada para o archive.org!' : label.textContent,
                                s.state === 'done' ? 'success' : 'error');
      }
      if (s.state === 'sending' || s.state === 'verifying') started = true;
    }

    function poll() {
      fetch(box.dataset.archiveStatus, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (s) {
          render(s);
          if (s.state === 'sending' || s.state === 'verifying') setTimeout(poll, 2000);
        })
        .catch(function () { setTimeout(poll, 10000); });
    }
    poll();
  }

  document.querySelectorAll('.archive-progress[data-archive-status]').forEach(setup);
})();
