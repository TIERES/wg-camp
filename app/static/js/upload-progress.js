// Formulários com ISO (form[data-upload] = tamanho máximo em bytes): confere o
// tamanho antes de enviar e mostra o progresso. Num envio comum o navegador
// fica minutos sem dizer nada. A resposta (página de destino depois do
// redirect, ou o formulário com o erro) substitui a página atual.
(function () {
  document.querySelectorAll('form[data-upload]').forEach(function (form) {
    // O arquivo da ISO (o formulário pode ter também a imagem dela).
    var input = form.querySelector('input[type=file][name=iso]') || form.querySelector('input[type=file]');
    var box = form.querySelector('.upload-progress');
    var max = Number(form.dataset.upload);
    form.addEventListener('submit', function (e) {
      var file = input.files[0];
      if (!file) return;
      e.preventDefault();
      if (file.size > max) {
        toast('A ISO pode ter no máximo ' + Math.floor(max / 1048576) + ' MB.', 'error');
        return;
      }
      var button = form.querySelector('button');
      var bar = box.querySelector('progress');
      var label = box.querySelector('span');
      var xhr = new XMLHttpRequest();
      xhr.open('POST', form.action);
      xhr.upload.onprogress = function (ev) {
        if (!ev.lengthComputable) return;
        var pct = Math.floor(ev.loaded * 100 / ev.total);
        bar.value = pct;
        label.textContent = pct < 100 ? 'Enviando ' + (form.dataset.uploadLabel || 'a ISO') + '... ' + pct + '%'
          : 'Conferindo o arquivo...';
      };
      xhr.onload = function () {
        var url = xhr.responseURL || form.action;
        document.open();
        document.write(xhr.responseText);
        document.close();
        history.replaceState(null, '', url);
      };
      xhr.onerror = function () {
        box.hidden = true;
        button.disabled = false;
        toast('O envio falhou. Confira a conexão e tente de novo.', 'error');
      };
      box.hidden = false;
      button.disabled = true;
      var data = new FormData(form);
      // O botão clicado (ex.: "Liberar ISO") não entra no FormData sozinho.
      if (e.submitter && e.submitter.name) data.append(e.submitter.name, e.submitter.value);
      xhr.send(data);
    });
  });
})();
