const sendButton = document.getElementById("sendButton");
const form = document.querySelector(".form");
const chatInput = document.getElementById("chatInput");
const chatWindow = document.getElementById("chatWindow");
const aiResult = document.getElementById("aiResult");
const csrfToken = document.querySelector('meta[name="csrf_token"]').content;

function addMessage(text, kind) {
  const item = document.createElement("div");
  item.className = kind === "user" ? "user-message" : "bot-message";
  item.textContent = text;
  chatWindow.appendChild(item);
  chatWindow.scrollTop = chatWindow.scrollHeight;
}

async function sendQuestion() {
  const message = chatInput.value.trim();
  if (!message) return;

  addMessage(message, "user");
  chatInput.value = "";

  sendButton.disabled = true;
  sendButton.textContent = "Думаю...";
  aiResult.classList.add("hidden");

  try {
    const response = await fetch("/ask", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken
      },
      body: JSON.stringify({ message: message })
    });

    const text = await response.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch (error) {
      console.error("Сервер вернул не JSON:", text);
      throw new Error("Сервер вернул HTML вместо JSON. Проверь Flask.");
    }

    if (!response.ok) {
      throw new Error(data.error || `Ошибка сервера: ${response.status}`);
    }

    addMessage(data.answer, "bot");

    aiResult.classList.remove("hidden");
    aiResult.innerHTML = "";

    const title = document.createElement("b");
    title.textContent = "Результат анализа: ";

    const value = document.createElement("span");
    const labels = {
      question: "Информационный вопрос",
      fault: "Неисправность",
      danger: "Потенциальная опасность",
      unclear: "Нужно уточнение",
      unknown: "Нет проверенного ответа"
    };
    value.textContent = labels[data.type] || "Обращение";

    aiResult.append(title, value);

    if (["fault", "danger"].includes(data.type)) {
      document.getElementById("ticketTitle").value = data.title || message.slice(0, 80);
      document.getElementById("ticketDescription").value = message;
      document.getElementById("ticketCategory").value = data.category || "Другое";
      document.getElementById("ticketDescription").focus();
    }
  } catch (error) {
    addMessage(error.message, "bot");
  } finally {
    sendButton.disabled = false;
    sendButton.textContent = "Отправить";
  }
}

sendButton?.addEventListener("click", sendQuestion);

chatInput?.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendQuestion();
  }
});
