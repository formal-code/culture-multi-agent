"""PyCharm 直接运行入口。"""
from culture.collector import collect
from culture.types import CollectionError, ModelUnavailable


def main():
    print("聊城文旅素材采集器")
    print("本次会调用百炼模型，可能产生费用；遇到接口权限、额度或服务错误会停止。")
    try:
        request = input("请输入素材需求（如：聊城东昌府区光岳楼和东昌湖的文旅图片和视频）：\n").strip()
        report = collect(request)
        counts = report["approved_counts"]
        print(f"\n本轮结果：{report['status']}；通过 {counts['total']} 份（图片 {counts['image']}，视频 {counts['video']}）")
        print(f"候选尝试：{report['candidates_attempted']}/{report['limits']['candidates']}；不设最低数量")
        if report.get("reason"):
            print("结束原因：" + report["reason"])
        print("输出目录：data/聊城")
        print("优先查看 research.json、fact.json、sources.json 和 collection_report.json")
    except (CollectionError, ModelUnavailable, ValueError) as exc:
        print("\n运行停止：" + str(exc))
        print("模型中断前已通过的素材会保存；本轮没有合格素材时保留上轮共享素材。")
    except KeyboardInterrupt:
        print("\n已由用户停止。")


if __name__ == "__main__":
    main()
