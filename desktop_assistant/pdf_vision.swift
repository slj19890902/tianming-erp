import Foundation
import Vision
import ImageIO
let raw=FileHandle.standardInput.readDataToEndOfFile()
guard raw.count<=48*1024*1024,let inputs=try JSONSerialization.jsonObject(with:raw) as? [[String:Any]],inputs.count<=100 else {exit(2)}
var pages:[[[String:Any]]]=[]
for input in inputs {
 guard let base64=input["image"] as? String,let bytes=Data(base64Encoded:base64),let source=CGImageSourceCreateWithData(bytes as CFData,nil),let image=CGImageSourceCreateImageAtIndex(source,0,nil),image.width*image.height<40_000_000 else {exit(3)}
 let request=VNRecognizeTextRequest();request.recognitionLevel = .accurate;request.recognitionLanguages=(input["latin"] as? Bool)==true ? ["en-US"] : ["zh-Hans","en-US"];request.usesLanguageCorrection=false
 try VNImageRequestHandler(cgImage:image).perform([request]);var blocks:[[String:Any]]=[]
 for ob in request.results ?? [] {
  guard let c=ob.topCandidates(1).first else {continue};let r=ob.boundingBox
  let x=r.minX*Double(image.width),y=(1-r.maxY)*Double(image.height),w=r.width*Double(image.width),h=r.height*Double(image.height)
  blocks.append(["text":c.string,"confidence":Double(c.confidence),"box":[[x,y],[x+w,y],[x+w,y+h],[x,y+h]]])
 }
 pages.append(blocks)
}
FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject:pages,options:[.sortedKeys]))
