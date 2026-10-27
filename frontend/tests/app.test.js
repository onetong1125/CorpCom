import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import App from "../src/App.vue";
import MarkdownContent from "../src/components/MarkdownContent.vue";
import ArtifactPanel from "../src/components/ArtifactPanel.vue";

const workspace = {
  id: "w1",
  title: "经营韧性研究",
  goal: "比较经营现金流",
  initial_hypothesis: "现金流稳定",
  scope: "两家公司",
  observation_period: "2023—2025",
  preferences: { 报告语言: "中文" },
  threads: [{ id: "t1", workspace_id: "w1", title: "首次讨论" }],
  artifacts: [],
};
const savedUser = {
  id: 1,
  role: "user",
  content: "请生成报告",
  created_at: "2026-10-26T00:00:00Z",
};
let wrapper;
let messages;
let detail;
let posted;
let postHandler;

function response(value, status = 200, asText = false) {
  return new Response(asText ? value : JSON.stringify(value), {
    status,
    headers: { "Content-Type": asText ? "text/plain" : "application/json" },
  });
}

beforeEach(() => {
  window.history.replaceState(null, "", "/");
  vi.stubGlobal("scrollTo", vi.fn());
  Element.prototype.scrollIntoView = vi.fn();
  HTMLDialogElement.prototype.showModal = function () {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function () {
    this.open = false;
  };
  messages = [];
  detail = structuredClone(workspace);
  posted = [];
  postHandler = async () => response({ content: "已完成" }, 201);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, options = {}) => {
      if (options.method === "POST" || options.method === "PUT") {
        posted.push({ url, body: JSON.parse(options.body) });
        return postHandler(url, options);
      }
      if (url === "/api/workspaces") return response([detail]);
      if (url === "/api/workspaces/w1") return response(detail);
      if (url === "/api/workspaces/w1/threads/t1")
        return response({ ...detail.threads[0], messages });
      if (url.endsWith("/content"))
        return response("# 报告 v1\n\n已有研究证据", 200, true);
      throw new Error(`Unexpected request: ${url}`);
    }),
  );
});

afterEach(() => {
  wrapper?.unmount();
  vi.unstubAllGlobals();
});

async function openApp() {
  wrapper = mount(App, { attachTo: document.body });
  await flushPromises();
  return wrapper;
}

async function send() {
  await wrapper.get('textarea[aria-label="研究问题"]').setValue("请生成报告");
  await wrapper.get("form.composer").trigger("submit");
  await flushPromises();
}

describe("research workbench", () => {
  it("loads persisted workspace and thread, preserving their IDs in the URL", async () => {
    messages = [savedUser];
    await openApp();
    expect(wrapper.get("h1").text()).toBe("经营韧性研究");
    expect(wrapper.get(".user-content").text()).toBe("请生成报告");
    expect(window.location.search).toBe("?workspace=w1&thread=t1");
  });

  it("disables duplicate sends and navigation while pending, then refreshes messages and artifacts", async () => {
    let finish;
    postHandler = () =>
      new Promise((resolve) => {
        finish = resolve;
      });
    await openApp();
    await send();
    expect(
      wrapper.get('button[aria-label="发送研究任务"]').attributes("disabled"),
    ).toBeDefined();
    expect(wrapper.get(".workspace-item").attributes("disabled")).toBeDefined();
    expect(wrapper.get('[role="status"]').text()).toContain("正在研究");
    await wrapper.get("form.composer").trigger("submit");
    expect(posted).toHaveLength(1);
    messages = [
      savedUser,
      { ...savedUser, id: 2, role: "assistant", content: "报告已归档" },
    ];
    detail.artifacts = [
      {
        id: "a1",
        name: "report.md",
        kind: "report",
        relative_path: "workspaces/w1/tasks/sample/v1/report.md",
        created_at: savedUser.created_at,
      },
    ];
    finish(response(messages[1], 201));
    await flushPromises();
    expect(wrapper.findAll(".message")).toHaveLength(2);
    expect(wrapper.text()).toContain("report.md");
    expect(wrapper.get('textarea[aria-label="研究问题"]').element.value).toBe(
      "",
    );
  });

  it("shows a failed request and reloads the saved user message without automatic retry", async () => {
    postHandler = async () => {
      messages = [savedUser];
      return response(
        { detail: "Model request failed; the user message was saved" },
        502,
      );
    };
    await openApp();
    await send();
    expect(posted).toHaveLength(1);
    expect(wrapper.get('[role="alert"]').text()).toContain("502");
    expect(wrapper.findAll(".message")).toHaveLength(1);
    expect(wrapper.get(".user-content").text()).toBe(savedUser.content);
    expect(wrapper.get('textarea[aria-label="研究问题"]').element.value).toBe(
      savedUser.content,
    );
    expect(wrapper.find(".researching").exists()).toBe(false);
  });

  it("distinguishes connection failure from an empty workspace list and supports retry", async () => {
    fetch.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await openApp();
    expect(wrapper.get('[role="alert"]').text()).toContain("无法连接后端");
    await wrapper.get(".error-banner .text-button").trigger("click");
    await flushPromises();
    expect(wrapper.get("h1").text()).toBe(workspace.title);
    expect(wrapper.find('[role="alert"]').exists()).toBe(false);
  });

  it("edits preferences inside the research archive and reloads saved values", async () => {
    await openApp();
    await wrapper.get(".meta-button").trigger("click");
    expect(wrapper.get("#dialog-title").text()).toBe("研究档案");
    expect(wrapper.get(".research-details").text()).toContain(workspace.goal);
    expect(wrapper.get(".research-details").text()).toContain(
      workspace.observation_period,
    );
    expect(
      JSON.parse(wrapper.get('textarea[name="preferences"]').element.value),
    ).toEqual(workspace.preferences);
    await wrapper.get('textarea[name="preferences"]').setValue("[]");
    await wrapper.get("dialog form").trigger("submit");
    expect(wrapper.get('dialog [role="alert"]').text()).toContain("JSON 对象");
    expect(posted).toHaveLength(0);
    postHandler = async (url, options) => {
      detail.preferences = JSON.parse(options.body).preferences;
      return response(detail);
    };
    await wrapper
      .get('textarea[name="preferences"]')
      .setValue('{"报告语言":"中文","关注重点":"现金流"}');
    await wrapper.get("dialog form").trigger("submit");
    await flushPromises();
    expect(posted[0]).toEqual({
      url: "/api/workspaces/w1/preferences",
      body: { preferences: { 报告语言: "中文", 关注重点: "现金流" } },
    });
    expect(wrapper.get("dialog").element.open).toBe(false);
    await wrapper.get(".meta-button").trigger("click");
    expect(
      JSON.parse(wrapper.get('textarea[name="preferences"]').element.value),
    ).toEqual(detail.preferences);
  });

  it("closes a successfully created form even if refreshing the new workspace fails", async () => {
    await openApp();
    await wrapper.get(".new-workspace").trigger("click");
    await wrapper.get('input[name="title"]').setValue("新研究");
    await wrapper.get('textarea[name="goal"]').setValue("新目标");
    await wrapper.get('textarea[name="initial_hypothesis"]').setValue("待验证");
    postHandler = async () => response({ id: "w2" }, 201);
    await wrapper.get("dialog form").trigger("submit");
    await flushPromises();
    expect(wrapper.get("dialog").element.open).toBe(false);
    expect(wrapper.get('[role="alert"]').text()).toContain(
      "已保存，但刷新失败",
    );
    expect(posted).toHaveLength(1);
  });
});

describe("artifact rendering", () => {
  it("renders Markdown tables but strips executable HTML and unsafe links", () => {
    wrapper = mount(MarkdownContent, {
      props: {
        content:
          '# 研究报告\n\n| 年份 | 比率 |\n| --- | --- |\n| 2025 | 1.2 |\n\n<script>alert(1)</script><img src="x" onerror="alert(2)"><a href="javascript:alert(3)">bad</a>',
      },
    });
    expect(wrapper.find("table").exists()).toBe(true);
    expect(wrapper.find("script").exists()).toBe(false);
    expect(wrapper.html()).not.toContain("onerror");
    expect(wrapper.html()).not.toContain("javascript:");
  });

  it("keeps both version directories and previews Markdown via the content endpoint", async () => {
    detail.artifacts = ["v1", "v2"].map((version) => ({
      id: version,
      name: "report.md",
      kind: "report",
      relative_path: `workspaces/w1/tasks/sample/${version}/report.md`,
      created_at: savedUser.created_at,
    }));
    wrapper = mount(ArtifactPanel, { props: { workspace: detail } });
    expect(wrapper.find('details[data-path="tasks/sample/v1"]').exists()).toBe(
      true,
    );
    expect(wrapper.find('details[data-path="tasks/sample/v2"]').exists()).toBe(
      true,
    );
    await wrapper.findAll(".file-row")[0].trigger("click");
    await flushPromises();
    expect(wrapper.get(".markdown h1").text()).toBe("报告 v1");
    expect(wrapper.get("a[download]").attributes("href")).toBe(
      "/api/workspaces/w1/files/v2/content",
    );
  });
});

it("reports a missing artifact and retries through its content endpoint", async () => {
  detail.artifacts = [
    {
      id: "a1",
      name: "report.md",
      kind: "report",
      relative_path: "workspaces/w1/tasks/sample/v1/report.md",
      created_at: savedUser.created_at,
    },
  ];
  fetch.mockResolvedValueOnce(
    response({ detail: "Stored file is missing" }, 404),
  );
  wrapper = mount(ArtifactPanel, { props: { workspace: detail } });
  await wrapper.get(".file-row").trigger("click");
  await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toContain("404");
  await wrapper.get('[role="alert"] button').trigger("click");
  await flushPromises();
  expect(wrapper.get(".markdown h1").text()).toBe("报告 v1");
});

it("aborts an outstanding preview when leaving the file, without showing an error", async () => {
  detail.artifacts = [
    {
      id: "a1",
      name: "report.md",
      kind: "report",
      relative_path: "workspaces/w1/tasks/sample/v1/report.md",
      created_at: savedUser.created_at,
    },
  ];
  let signal;
  fetch.mockImplementationOnce((url, options) => {
    signal = options.signal;
    return new Promise((resolve, reject) =>
      signal.addEventListener("abort", () =>
        reject(new DOMException("Aborted", "AbortError")),
      ),
    );
  });
  wrapper = mount(ArtifactPanel, { props: { workspace: detail } });
  await wrapper.get(".file-row").trigger("click");
  await wrapper.get(".preview-toolbar button").trigger("click");
  await flushPromises();
  expect(signal.aborted).toBe(true);
  expect(wrapper.find(".file-row").exists()).toBe(true);
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
});

it("provides nested collapsible artifact directories and preserves them after preview", async () => {
  detail.artifacts = [
    {
      id: "old",
      name: "report.md",
      kind: "report",
      relative_path: "workspaces/w1/runs/run-1/reports/report.md",
      created_at: savedUser.created_at,
    },
    {
      id: "new",
      name: "report.md",
      kind: "report",
      relative_path: "workspaces/w1/tasks/sample/v2/report.md",
      created_at: savedUser.created_at,
    },
  ];
  wrapper = mount(ArtifactPanel, { props: { workspace: detail } });
  expect(wrapper.text()).not.toContain("同一研究的所有会话共享资料");
  const runs = wrapper.get('details[data-path="runs"]');
  const run = wrapper.get('details[data-path="runs/run-1"]');
  const reports = wrapper.get('details[data-path="runs/run-1/reports"]');
  for (const directory of [runs, run, reports]) {
    expect(directory.element.open).toBe(false);
    directory.element.open = true;
    await directory.trigger("toggle");
  }
  await reports.get(".file-row").trigger("click");
  await flushPromises();
  expect(wrapper.get("a[download]").attributes("href")).toBe(
    "/api/workspaces/w1/files/old/content",
  );
  await wrapper.get(".preview-toolbar button").trigger("click");
  expect(
    wrapper.get('details[data-path="runs/run-1/reports"]').element.open,
  ).toBe(true);
  runs.element.open = false;
  await runs.trigger("toggle");
  expect(run.element.open).toBe(true);
});

it("leaves an existing empty conversation blank with an enabled composer", async () => {
  await openApp();
  expect(wrapper.find(".chat-empty").exists()).toBe(false);
  expect(wrapper.text()).not.toContain("梳理当前研究");
  expect(
    wrapper.get('textarea[aria-label="研究问题"]').attributes("disabled"),
  ).toBeUndefined();
});

it("creates a workspace with only a title and leaves goal and hypothesis optional", async () => {
  await openApp();
  await wrapper.get(".new-workspace").trigger("click");
  expect(wrapper.get('input[name="title"]').element.required).toBe(true);
  expect(wrapper.get('textarea[name="goal"]').element.required).toBe(false);
  expect(
    wrapper.get('textarea[name="initial_hypothesis"]').element.required,
  ).toBe(false);
  await wrapper.get('input[name="title"]').setValue("仅标题的研究");
  postHandler = async (url, options) => {
    detail = {
      ...detail,
      ...JSON.parse(options.body),
      threads: [],
      artifacts: [],
    };
    return response(detail, 201);
  };
  await wrapper.get("dialog form").trigger("submit");
  await flushPromises();
  expect(posted[0]).toEqual({
    url: "/api/workspaces",
    body: {
      title: "仅标题的研究",
      goal: "",
      initial_hypothesis: "",
      scope: "",
      observation_period: "",
      preferences: {},
    },
  });
  expect(wrapper.get("dialog").element.open).toBe(false);
  expect(wrapper.get("h1").text()).toBe("仅标题的研究");
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
});
