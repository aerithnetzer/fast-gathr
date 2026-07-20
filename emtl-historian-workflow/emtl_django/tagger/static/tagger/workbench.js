(function () {
  document.documentElement.classList.add("js-enabled");

  document.querySelectorAll(".auto-upload-form").forEach((form) => {
    const input = form.querySelector('input[type="file"][name="document_file"]');
    const status = form.querySelector(".upload-status-text");
    if (!input) {
      return;
    }
    input.addEventListener("change", () => {
      if (!input.files || !input.files.length) {
        return;
      }
      if (status) {
        status.textContent = `Uploading ${input.files[0].name}...`;
      }
      form.classList.add("is-uploading");
      form.submit();
    });
  });

  const detailBox = document.getElementById("annotation-detail");
  if (!detailBox) {
    return;
  }

  function showDetail(target) {
    const raw = target.getAttribute("data-detail");
    if (!raw) {
      return;
    }
    try {
      const detail = JSON.parse(raw);
      detailBox.hidden = false;
      detailBox.innerHTML = `
        <strong>${detail.type || "Tag"} | ${detail.headword || "Untitled"}</strong>
        <span>${detail.stable_id || "No ID"} | ${detail.stage || "fixture"} | ${detail.review || "review"}</span>
        <p>${detail.note || "No note supplied."}</p>
      `;
    } catch (error) {
      detailBox.hidden = false;
      detailBox.textContent = raw;
    }
  }

  document.querySelectorAll(".annotation").forEach((node) => {
    node.addEventListener("mouseenter", () => showDetail(node));
    node.addEventListener("focus", () => showDetail(node));
    node.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      showDetail(node);
    });
  });
})();
