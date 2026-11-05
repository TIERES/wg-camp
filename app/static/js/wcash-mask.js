// Campos de W$ (input[data-wcash]): digita-se só os números e o valor enche
// da direita para a esquerda, como numa maquininha: 2 -> W$ 0,02,
// 200 -> W$ 2,00. O servidor (wallet.parse_cents) aceita o "W$ " e os pontos.
// Com data-wcash-signed, "-" deixa o valor negativo e "+" volta ao positivo
// (o último sinal digitado vale): -W$ 0,50.
(function () {
  var MAX_DIGITS = 11; // parse_cents: até 9 dígitos antes da vírgula

  function format(input) {
    var value = input.value;
    var negative = input.hasAttribute('data-wcash-signed') && value.lastIndexOf('-') > value.lastIndexOf('+');
    var digits = value.replace(/\D/g, '');
    if (!digits) { input.value = negative ? '-' : ''; return; }
    digits = digits.replace(/^0+/, '').slice(0, MAX_DIGITS);
    while (digits.length < 3) digits = '0' + digits;
    var units = digits.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, '.');
    input.value = (negative ? '-' : '') + 'W$ ' + units + ',' + digits.slice(-2);
  }

  document.querySelectorAll('input[data-wcash]').forEach(function (input) {
    format(input);
    input.addEventListener('input', function () {
      format(input);
      input.setSelectionRange(input.value.length, input.value.length);
    });
  });
})();
