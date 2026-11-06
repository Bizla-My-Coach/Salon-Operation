(function () {
  // Narrow a long <select> as the user types in its search box (data-filters="<select id>").
  // Every typed word must appear in the option text, in any order: "gold o3" finds "O3+ Gold Infusion Facial".
  // Listeners are delegated so boxes keep working after the live refresh swaps the page section.
  var originals = new WeakMap();

  function words(value) {
    return (value || "").toLowerCase().replace(/\s+/g, " ").trim().split(" ").filter(Boolean);
  }

  function parts(input) {
    var select = document.getElementById(input.getAttribute("data-filters"));
    if (!select) {
      return null;
    }
    if (!originals.has(select)) {
      var prompt = select.options.length && !select.options[0].value ? select.options[0] : null;
      var all = Array.prototype.filter.call(select.options, function (option) { return option !== prompt; });
      originals.set(select, {prompt: prompt, all: all});
    }
    return {select: select, count: document.getElementById(input.getAttribute("data-count")), data: originals.get(select)};
  }

  function render(input) {
    var found = parts(input);
    if (!found) {
      return;
    }
    var select = found.select, data = found.data, query = words(input.value), previous = select.value;
    var matches = data.all.filter(function (option) {
      var text = option.text.toLowerCase();
      return query.every(function (word) { return text.indexOf(word) !== -1; });
    });
    while (select.options.length) {
      select.remove(0);
    }
    if (data.prompt) {
      select.add(data.prompt);
    }
    matches.forEach(function (option) { select.add(option); });
    var kept = matches.some(function (option) { return option.value === previous; });
    select.value = kept ? previous : (query.length && matches.length ? matches[0].value : "");
    if (found.count) {
      if (!found.count.hasAttribute("data-total")) {
        found.count.setAttribute("data-total", found.count.textContent);
      }
      found.count.textContent = query.length ? matches.length + " of " + data.all.length + " match"
        : found.count.getAttribute("data-total");
    }
  }

  document.addEventListener("input", function (event) {
    if (event.target.matches && event.target.matches("input[data-filters]")) {
      render(event.target);
    }
  });
  document.addEventListener("keydown", function (event) {
    var input = event.target;
    if (event.key !== "Enter" || !input.matches || !input.matches("input[data-filters]")) {
      return;
    }
    // Enter opens the highlighted match instead of submitting an empty choice.
    event.preventDefault();
    var found = parts(input);
    if (found && found.select.value && found.select.form) {
      if (found.select.form.requestSubmit) {
        found.select.form.requestSubmit();
      } else {
        found.select.form.submit();
      }
    }
  });
}());
