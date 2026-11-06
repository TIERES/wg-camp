// Formulários com ISO (form[data-upload] = tamanho máximo em bytes): confere o
// tamanho antes de enviar e mostra o progresso. Num envio comum o navegador
// fica minutos sem dizer nada. A resposta (página de destino depois do
// redirect, ou o formulário com o erro) substitui a página atual.
(function () {
  document.querySelectorAll('form[data-upload]').forEach(function (form) {
    var input = form.querySelector('input[type=file]');
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
        label.textContent = pct < 100 ? 'Enviando a ISO... ' + pct + '%' : 'Conferindo o arquivo...';
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
      xhr.send(new FormData(form));
    });
  });
})();
