// 只用固定模板生成弹窗，动态标题和消息用 textContent 写入。关闭和 Esc 按取消处理。
const dialog = document.createElement("dialog");
dialog.className = "surface-dialog confirm-dialog";
dialog.setAttribute("aria-labelledby", "workspace-dialog-title");
dialog.setAttribute("aria-describedby", "workspace-dialog-message");
dialog.innerHTML = `
  <form method="dialog">
    <h2 id="workspace-dialog-title"></h2>
    <p id="workspace-dialog-message"></p>
    <footer>
      <button type="submit" class="control" value="cancel">取消</button>
      <button type="submit" class="control primary-control" value="accept">确定</button>
    </footer>
  </form>
`;
const cancel = dialog.querySelector('[value="cancel"]');
const accept = dialog.querySelector('[value="accept"]');
let pending = false;

export function askWorkspace({ title, message, action = "确定", dismissOnly = false }) {
  if (pending) return Promise.resolve(false);
  pending = true;
  if (!dialog.isConnected) document.body.append(dialog);
  dialog.querySelector("h2").textContent = title;
  dialog.querySelector("p").textContent = message;
  cancel.hidden = dismissOnly;
  accept.textContent = action;
  dialog.returnValue = "";
  dialog.showModal();
  (dismissOnly ? accept : cancel).focus();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      pending = false;
      resolve(dialog.returnValue === "accept");
    }, { once: true });
  });
}
