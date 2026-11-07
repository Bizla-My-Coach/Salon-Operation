(function () {
  // One box to search and choose a service: type to narrow, click or press Enter to open the plan.
  // Every typed word must appear in the name, category or code, in any order ("raaga pedi").
  function words(value) {
    return (value || "").toLowerCase().replace(/\s+/g, " ").trim().split(" ").filter(Boolean);
  }

  function setup(box) {
    var input = box.querySelector(".service-combobox-input");
    var menu = box.querySelector(".service-combobox-menu");
    var select = box.querySelector("select");
    if (!input || !menu || !select || box.classList.contains("combobox-ready")) {
      return;
    }
    var options = [];
    for (var i = 0; i < select.options.length; i += 1) {
      var option = select.options[i];
      if (option.value) {
        options.push({value: option.value, text: option.text});
        if (option.selected) {
          input.value = option.text;
        }
      }
    }
    var label = document.getElementById(input.getAttribute("aria-labelledby"));
    if (label) {
      label.setAttribute("for", input.id);
    }
    input.required = select.required;
    select.required = false;
    box.classList.add("combobox-ready");
    var active = -1;

    function close() {
      menu.innerHTML = "";
      menu.style.display = "none";
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }

    function choose(option) {
      select.value = option.value;
      input.value = option.text;
      close();
      if (select.form) {
        if (select.form.requestSubmit) {
          select.form.requestSubmit();
        } else {
          select.form.submit();
        }
      }
    }

    function matches() {
      var query = words(input.value === selectedText() ? "" : input.value);
      return options.filter(function (option) {
        var text = option.text.toLowerCase();
        return query.every(function (word) { return text.indexOf(word) !== -1; });
      });
    }

    function selectedText() {
      var current = select.options[select.selectedIndex];
      return current && current.value ? current.text : null;
    }

    function render() {
      var found = matches();
      menu.innerHTML = "";
      active = -1;
      var head = document.createElement("div");
      head.className = "combobox-empty";
      head.textContent = found.length ? found.length + " of " + options.length + " services" : "No matching service";
      menu.appendChild(head);
      found.forEach(function (option) {
        var item = document.createElement("button");
        item.type = "button";
        item.className = "combobox-option";
        item.id = menu.id + "-" + option.value;
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", "false");
        item.textContent = option.text;
        item.addEventListener("mousedown", function (event) { event.preventDefault(); });
        item.addEventListener("click", function () { choose(option); });
        menu.appendChild(item);
      });
      menu.style.display = "block";
      input.setAttribute("aria-expanded", "true");
    }

    function highlight(step) {
      var items = menu.querySelectorAll(".combobox-option");
      if (!items.length) {
        return;
      }
      active = (active + step + items.length) % items.length;
      for (var j = 0; j < items.length; j += 1) {
        items[j].classList.toggle("active", j === active);
        items[j].setAttribute("aria-selected", j === active ? "true" : "false");
      }
      input.setAttribute("aria-activedescendant", items[active].id);
      items[active].scrollIntoView({block: "nearest"});
    }

    input.addEventListener("focus", function () { input.select(); render(); });
    input.addEventListener("click", render);
    input.addEventListener("input", function () { select.value = ""; render(); });
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (menu.style.display !== "block") { render(); }
        highlight(1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        highlight(-1);
      } else if (event.key === "Enter") {
        event.preventDefault();
        var items = menu.querySelectorAll(".combobox-option");
        if (active >= 0 && items[active]) {
          items[active].click();
        } else if (items.length) {
          items[0].click();
        }
      } else if (event.key === "Escape") {
        close();
      }
    });
    input.addEventListener("blur", function () {
      setTimeout(function () {
        if (!box.contains(document.activeElement)) {
          close();
          if (!select.value) {
            input.value = "";
          }
        }
      }, 200);
    });
  }

  function setupAll() {
    document.querySelectorAll(".catalogue-combobox").forEach(setup);
  }

  setupAll();
  // The page's live refresh can swap this section; set up any new box it brings in.
  new MutationObserver(setupAll).observe(document.body, {childList: true, subtree: true});
}());
