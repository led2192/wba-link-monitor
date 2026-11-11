// Send to lanterN: a click on the button opens the sending window for the tab you are on.
chrome.action.onClicked.addListener(async tab => {
  const q = new URLSearchParams({ tab: String(tab.id), url: tab.url || "", title: tab.title || "" });
  await chrome.windows.create({ url: chrome.runtime.getURL("send.html") + "?" + q.toString(), type: "popup", width: 560, height: 760 });
});
